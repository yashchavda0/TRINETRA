"""FastAPI application entrypoint for Model 1 - Central GIS Camera Registry.

Run with:
    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from collections.abc import AsyncIterator
from typing import Any, Final

import asyncpg
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app import database
from app.config import get_settings
from app.routers import (
    alerts,
    audit,
    auth,
    cameras,
    detections,
    registry_io,
    reports,
    scene_events,
    streams,
    watchlist,
)
from app.schemas import HealthResponse

# Correlation id for the request currently being served, readable by every log
# record emitted anywhere in the call stack.
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

logger: Final = logging.getLogger("app")

# Attributes present on every LogRecord. Anything outside this set came from an
# `extra={...}` argument and is merged into the JSON payload.
_RESERVED_LOG_ATTRS: Final = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "taskName", "thread", "threadName",
    }
)


class JsonLogFormatter(logging.Formatter):
    """Render each log record as one line of JSON.

    Timestamps are UTC Unix epoch milliseconds, matching
    ``SurveillanceEvent.timestamp_utc_ms`` on the analytics bus, so log lines
    and events can be joined on time without timezone guesswork.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp_utc_ms": int(record.created * 1000),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", request_id_ctx.get()),
        }

        for key, value in record.__dict__.items():
            if key in _RESERVED_LOG_ATTRS or key in payload:
                continue
            payload[key] = value if isinstance(value, (str, int, float, bool, type(None))) else str(value)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(log_level: str) -> None:
    """Install the JSON formatter on the root logger and uvicorn's loggers."""
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonLogFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)

    # uvicorn installs its own handlers; strip them so every line on stdout is
    # JSON and nothing is emitted twice.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the database pool for the lifetime of the application."""
    settings = get_settings()
    configure_logging(settings.log_level)

    logger.info(
        "service starting",
        extra={
            "service": settings.app_name,
            "version": settings.app_version,
            "environment": settings.environment,
        },
    )

    # A shared signing key means anyone holding the source can mint an admin
    # token, so this must be loud rather than a comment nobody reads.
    if settings.jwt_secret == "trinetra-development-secret-change-me":
        logger.warning(
            "JWT_SECRET is still the built-in development value - set a unique "
            "secret before this service is reachable by anyone else"
        )

    # Deliberately not guarded: an unreachable database must abort startup
    # rather than let the service accept traffic it cannot serve.
    await database.connect(settings)

    # Keeps relay sessions alive because the console never calls the relay's
    # heartbeat route and the relay reaps after 30s. No-op without a relay.
    streams.start_keepalive(settings)
    # Brings every ACTIVE camera's MediaMTX path up continuously (not just
    # while someone is watching) so recording has something to record. No-op
    # unless MediaMTX is the configured backend.
    streams.start_recording_reconciler(settings)
    try:
        yield
    finally:
        await streams.stop_recording_reconciler()
        await streams.stop_keepalive()
        await database.disconnect()
        logger.info("service stopped", extra={"service": settings.app_name})


settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "Model 1 of the Gujarat Police CCTV Integration System: the authoritative "
        "spatial registry of the integrated camera fleet. All coordinates are "
        "WGS84 (EPSG:4326); all timestamps are UTC."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    # PATCH and DELETE are required: without them the browser's preflight fails
    # and the console's only camera-edit and decommission paths are unreachable.
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next: Any) -> Any:
    """Stamp a correlation id on the request, time it, and log the outcome."""
    incoming = request.headers.get("X-Request-ID")
    request_id = incoming or str(uuid.uuid4())
    token = request_id_ctx.set(request_id)
    started = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        logger.exception(
            "request failed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "duration_ms": duration_ms,
            },
        )
        request_id_ctx.reset(token)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "internal server error", "request_id": request_id},
            headers={"X-Request-ID": request_id},
        )

    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request completed",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "query": str(request.url.query),
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    request_id_ctx.reset(token)
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return FastAPI's field-level validation errors in the shared envelope."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": "request validation failed",
            "errors": json.loads(json.dumps(exc.errors(), default=str)),
            "request_id": request_id_ctx.get(),
        },
    )


@app.exception_handler(ValidationError)
async def pydantic_validation_exception_handler(
    request: Request, exc: ValidationError
) -> JSONResponse:
    """Catch model validation raised outside FastAPI's own parsing step."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": "payload validation failed",
            "errors": json.loads(json.dumps(exc.errors(), default=str)),
            "request_id": request_id_ctx.get(),
        },
    )


@app.exception_handler(asyncpg.PostgresError)
async def postgres_exception_handler(
    request: Request, exc: asyncpg.PostgresError
) -> JSONResponse:
    """Surface database faults as 503 without leaking DSN, SQL, or row data."""
    logger.error(
        "database error",
        extra={
            "request_id": request_id_ctx.get(),
            "path": request.url.path,
            "sqlstate": getattr(exc, "sqlstate", None),
        },
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "detail": "registry database is unavailable, retry shortly",
            "request_id": request_id_ctx.get(),
        },
    )


@app.exception_handler(RuntimeError)
async def runtime_exception_handler(request: Request, exc: RuntimeError) -> JSONResponse:
    """Pool-not-initialised and similar operational faults map to 503."""
    logger.error(
        "operational error",
        extra={"request_id": request_id_ctx.get(), "path": request.url.path, "error": str(exc)},
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "detail": "service is not ready to handle requests",
            "request_id": request_id_ctx.get(),
        },
    )


app.include_router(auth.router)
app.include_router(auth.admin_router)
# registry_io before cameras: its literal paths (/bulk, /export.csv,
# /import-template.csv) share the /api/v1/cameras prefix, and the camera
# router's UUID-constrained routes must not be given the chance to claim them.
app.include_router(registry_io.router)
app.include_router(cameras.router)
app.include_router(reports.router)
app.include_router(audit.router)
app.include_router(detections.router)
app.include_router(detections.ws_router)
app.include_router(scene_events.router)
app.include_router(watchlist.router)
app.include_router(streams.router)
app.include_router(alerts.router)
app.include_router(alerts.ws_router)


@app.get("/health", response_model=HealthResponse, tags=["operations"])
async def health() -> JSONResponse:
    """Liveness and readiness probe: verifies a real round trip to PostgreSQL."""
    database_state = "down"
    try:
        pool = database.get_pool()
        async with pool.acquire() as connection:
            await connection.fetchval("SELECT 1")
        database_state = "up"
    except (asyncpg.PostgresError, RuntimeError, OSError) as exc:
        logger.error("health check failed", extra={"error": str(exc)})

    body = HealthResponse(
        status="ok" if database_state == "up" else "degraded",
        service=settings.app_name,
        version=settings.app_version,
        database=database_state,
        timestamp_utc_ms=int(time.time() * 1000),
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK
        if database_state == "up"
        else status.HTTP_503_SERVICE_UNAVAILABLE,
        content=body.model_dump(),
    )
