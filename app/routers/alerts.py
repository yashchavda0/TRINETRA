"""Priority-0 threat alert fan-out.

Two surfaces, one flow:

* `POST /api/v1/alerts/publish` — the Model 4 worker (and anything else holding
  the API key) pushes a built alert in. The alert is written to `threat_alerts`
  for audit *before* it is fanned out, so an alert that reached an operator is
  always on the record.
* `WebSocket /alerts/p0` — operator consoles subscribe. This is the URL
  `P0_ALERT_WS_URL` should point at, replacing the unresolvable
  `ws://central-command/alerts/p0` default compiled into `AlertDispatcher`.

Fan-out is best-effort per subscriber: a socket that fails a send is dropped and
the remaining subscribers still receive the alert.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Any, Final

import asyncpg
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)

from app.config import Settings, get_settings
from app.database import get_connection
from app.schemas import AlertPublishRequest, AlertPublishResult

logger: Final = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])
ws_router = APIRouter(tags=["alerts"])

# Connected operator consoles. Guarded by a lock because a disconnect can land
# while a broadcast is iterating.
_subscribers: set[WebSocket] = set()
_subscribers_lock: Final = asyncio.Lock()

_INSERT_ALERT_SQL: Final = """
    INSERT INTO threat_alerts (
        alert_id, priority, classification, plate_number, camera_id,
        alert_geom, detected_at_utc_ms, dispatched_at_utc_ms, confidence,
        vehicle, subject, evidence, schema_version
    )
    VALUES (
        $1, $2, $3, $4, $5,
        CASE
            WHEN $6::double precision IS NULL OR $7::double precision IS NULL THEN NULL
            ELSE ST_SetSRID(ST_MakePoint($6, $7), 4326)
        END,
        $8, $9, $10,
        $11::jsonb, $12::jsonb, $13::jsonb, $14
    )
    ON CONFLICT (alert_id) DO NOTHING
    RETURNING alert_id
"""


def _authorise(
    settings: Settings,
    *,
    api_key_header: str | None,
    authorization: str | None,
    token: str | None = None,
) -> None:
    """Reject the caller unless it presents the configured alert channel key.

    With no key configured the channel is open — acceptable for a local
    development stack, and the reason `.env.example` sets one.
    """
    expected = settings.p0_alert_api_key
    if not expected:
        return

    presented = api_key_header or token
    if not presented and authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            presented = value.strip()

    if presented != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing alert channel credentials",
        )


async def broadcast(frame: dict[str, Any]) -> int:
    """Send one alert frame to every subscriber; returns how many received it."""
    payload = json.dumps(frame, separators=(",", ":"), default=str)

    async with _subscribers_lock:
        targets = list(_subscribers)

    delivered = 0
    dead: list[WebSocket] = []
    for socket in targets:
        try:
            await socket.send_text(payload)
            delivered += 1
        except Exception:
            dead.append(socket)

    if dead:
        async with _subscribers_lock:
            for socket in dead:
                _subscribers.discard(socket)
        logger.info("dropped %d unreachable alert subscriber(s)", len(dead))

    return delivered


def subscriber_count() -> int:
    """Number of currently connected consoles, for /health and logs."""
    return len(_subscribers)


@router.post(
    "/publish",
    response_model=AlertPublishResult,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Record a threat alert and fan it out to connected consoles",
)
async def publish_alert(
    payload: AlertPublishRequest,
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> AlertPublishResult:
    """Persist an alert, then push it to every subscribed console."""
    _authorise(settings, api_key_header=x_api_key, authorization=authorization)

    stored_id = await connection.fetchval(
        _INSERT_ALERT_SQL,
        payload.alert_id,
        payload.priority,
        payload.classification,
        payload.plate_number,
        payload.camera_id,
        payload.longitude,  # $6 -> ST_MakePoint X
        payload.latitude,  # $7 -> ST_MakePoint Y
        payload.detected_at,
        payload.dispatched_at,
        payload.confidence,
        json.dumps(payload.vehicle, default=str),
        json.dumps(payload.subject, default=str),
        json.dumps(payload.evidence, default=str),
        payload.schema_version,
    )

    frame = payload.model_dump(mode="json")
    delivered = await broadcast(frame)

    logger.info(
        "alert published",
        extra={
            "alert_id": payload.alert_id,
            "priority": payload.priority,
            "classification": payload.classification,
            "stored": stored_id is not None,
            "subscribers_notified": delivered,
        },
    )
    return AlertPublishResult(
        alert_id=payload.alert_id,
        stored=stored_id is not None,
        subscribers_notified=delivered,
    )


@ws_router.websocket("/alerts/p0")
async def alerts_socket(
    websocket: WebSocket,
    token: Annotated[str | None, Query()] = None,
) -> None:
    """Operator console subscription to the live P0 alert stream."""
    settings = get_settings()
    try:
        _authorise(
            settings,
            api_key_header=websocket.headers.get("x-api-key"),
            authorization=websocket.headers.get("authorization"),
            token=token,
        )
    except HTTPException:
        # Reject before the handshake completes so no unauthenticated socket is
        # ever added to the subscriber set.
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        logger.warning("rejected unauthorised alert subscriber")
        return

    await websocket.accept()
    async with _subscribers_lock:
        _subscribers.add(websocket)
    logger.info("alert subscriber connected", extra={"subscribers": len(_subscribers)})

    try:
        # The console is receive-only; this read exists purely to observe the
        # disconnect. Anything it sends is acknowledged and ignored.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("alert subscriber failed")
    finally:
        async with _subscribers_lock:
            _subscribers.discard(websocket)
        logger.info(
            "alert subscriber disconnected", extra={"subscribers": len(_subscribers)}
        )
