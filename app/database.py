"""asyncpg connection-pool lifecycle for the Model 1 Camera Registry.

One pool per process, created during application startup and closed during
shutdown. Handlers borrow a connection through the :func:`get_connection`
FastAPI dependency and never construct connections themselves.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Final

import asyncpg

from app.config import Settings, get_settings

logger: Final = logging.getLogger(__name__)

# Module-level pool holder. Populated by connect(), cleared by disconnect().
_pool: asyncpg.Pool | None = None


async def _init_connection(connection: asyncpg.Connection) -> None:
    """Per-connection setup applied by the pool to every new connection."""
    # Pin the session to UTC so TIMESTAMPTZ values are read and written in UTC
    # regardless of the server's or container's local timezone.
    await connection.execute("SET TIME ZONE 'UTC'")

    # PostGIS geometry has an extension-assigned OID that asyncpg does not know.
    # Registering a passthrough text codec means a query that selects a raw
    # geometry column yields its EWKT/EWKB hex text instead of raising
    # "unknown type". Registry queries normally project ST_X/ST_Y instead, so
    # this is a safety net; if the type is not resolvable the pool still works.
    for type_name in ("geometry", "geography"):
        try:
            await connection.set_type_codec(
                type_name,
                schema="public",
                encoder=str,
                decoder=str,
                format="text",
            )
        except asyncpg.PostgresError as exc:  # pragma: no cover - environment dependent
            logger.debug(
                "no text codec registered for type %s: %s", type_name, exc
            )
        except ValueError as exc:  # pragma: no cover - environment dependent
            logger.debug(
                "type %s not present in schema public: %s", type_name, exc
            )


async def connect(settings: Settings | None = None) -> asyncpg.Pool:
    """Create the process-wide pool and verify the database is reachable.

    Raises the underlying asyncpg/OSError on failure so startup aborts loudly
    rather than serving traffic against a dead database.
    """
    global _pool

    if _pool is not None:
        return _pool

    settings = settings or get_settings()

    pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        max_inactive_connection_lifetime=settings.db_pool_max_inactive_connection_lifetime,
        command_timeout=settings.db_command_timeout,
        init=_init_connection,
    )

    # Fail fast: prove the credentials work and PostGIS is actually installed
    # before the service reports itself ready.
    async with pool.acquire() as connection:
        postgis_version = await connection.fetchval("SELECT PostGIS_Version()")

    _pool = pool
    logger.info(
        "database pool ready",
        extra={
            "database": settings.safe_database_target,
            "pool_min_size": settings.db_pool_min_size,
            "pool_max_size": settings.db_pool_max_size,
            "postgis_version": postgis_version,
        },
    )
    return _pool


async def disconnect() -> None:
    """Close the pool, waiting for in-flight queries to finish."""
    global _pool

    if _pool is None:
        return

    pool, _pool = _pool, None
    await pool.close()
    logger.info("database pool closed")


def get_pool() -> asyncpg.Pool:
    """Return the live pool, or raise if the service was not started properly."""
    if _pool is None:
        raise RuntimeError(
            "database pool is not initialised; connect() must run during startup"
        )
    return _pool


async def get_connection() -> AsyncIterator[asyncpg.Connection]:
    """FastAPI dependency yielding a pooled connection for one request."""
    pool = get_pool()
    async with pool.acquire() as connection:
        yield connection
