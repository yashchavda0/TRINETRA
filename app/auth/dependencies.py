"""FastAPI dependencies for authentication, authorisation and audit context."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Final

import asyncpg
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError

from app.auth.security import ACCESS_TOKEN_TYPE, decode_token, hash_api_key
from app.config import Settings, get_settings
from app.database import get_connection

logger: Final = logging.getLogger(__name__)

# auto_error=False so a missing header reaches our own handler and produces a
# consistent error envelope instead of FastAPI's bare 403.
_bearer = HTTPBearer(auto_error=False)

# Ordered least to most privileged; membership comparisons use the index.
ROLE_ORDER: Final = ("VIEWER", "OPERATOR", "DEPT_ADMIN", "SUPER_ADMIN")


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is making this request, and what they are allowed to touch."""

    id: str
    role: str
    department_id: str | None
    label: str
    is_service: bool = False

    @property
    def is_super_admin(self) -> bool:
        return self.role == "SUPER_ADMIN"

    def at_least(self, role: str) -> bool:
        """True when this principal's role is `role` or more privileged."""
        try:
            return ROLE_ORDER.index(self.role) >= ROLE_ORDER.index(role)
        except ValueError:
            return False

    @property
    def department_scope(self) -> str | None:
        """The department every query must be restricted to, or None for all.

        This is the single source of scope. Endpoints must apply it rather than
        trusting a department_id supplied in the request.
        """
        return None if self.is_super_admin else self.department_id

    def assert_can_access_department(self, department_id: str | None) -> None:
        """Reject a cross-department write attempt."""
        scope = self.department_scope
        if scope is None or department_id is None:
            return
        if department_id.upper() != scope.upper():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"your account is scoped to department '{scope}' and cannot "
                    f"act on '{department_id}'"
                ),
            )


def _unauthorised(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _principal_from_api_key(
    api_key: str, connection: asyncpg.Connection
) -> Principal | None:
    """Resolve a service API key. Returns None when it does not match."""
    record = await connection.fetchrow(
        """
        SELECT id, name, role, department_id, expires_at
        FROM api_keys
        WHERE key_hash = $1 AND is_active
        """,
        hash_api_key(api_key),
    )
    if record is None:
        return None

    expires_at = record["expires_at"]
    if expires_at is not None and expires_at <= datetime.now(timezone.utc):
        raise _unauthorised("api key has expired")

    # Best-effort usage stamp; never fail the request because this write failed.
    try:
        await connection.execute(
            "UPDATE api_keys SET last_used_at = clock_timestamp() WHERE id = $1",
            record["id"],
        )
    except asyncpg.PostgresError:
        logger.debug("could not stamp api key usage", exc_info=True)

    return Principal(
        id=str(record["id"]),
        role=record["role"],
        department_id=record["department_id"],
        label=f"service:{record['name']}",
        is_service=True,
    )


async def get_current_principal(
    request: Request,
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> Principal:
    """Resolve the caller from a bearer JWT or an X-API-Key header."""
    api_key = request.headers.get("X-API-Key")
    if api_key:
        principal = await _principal_from_api_key(api_key, connection)
        if principal is None:
            raise _unauthorised("invalid api key")
        return principal

    if credentials is None or not credentials.credentials:
        raise _unauthorised("authentication required")

    try:
        payload = decode_token(credentials.credentials, ACCESS_TOKEN_TYPE, settings)
    except InvalidTokenError as exc:
        raise _unauthorised(f"invalid token: {exc}") from exc

    user_id = payload["sub"]
    # The user row is re-read rather than trusted from the token: deactivating
    # an account must take effect immediately, not at token expiry.
    record = await connection.fetchrow(
        "SELECT id, full_name, email, role, department_id, is_active FROM users WHERE id = $1",
        user_id,
    )
    if record is None or not record["is_active"]:
        raise _unauthorised("account is inactive or no longer exists")

    return Principal(
        id=str(record["id"]),
        role=record["role"],
        department_id=record["department_id"],
        label=f"{record['full_name']} <{record['email']}>",
    )


def require_role(minimum: str):
    """Dependency factory enforcing a minimum role.

    Usage: ``Depends(require_role("DEPT_ADMIN"))``.
    """
    if minimum not in ROLE_ORDER:
        raise ValueError(f"unknown role: {minimum}")

    async def _guard(
        principal: Annotated[Principal, Depends(get_current_principal)],
    ) -> Principal:
        if not principal.at_least(minimum):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"this action requires the {minimum} role or higher",
            )
        return principal

    return _guard


async def get_audited_connection(
    principal: Annotated[Principal, Depends(get_current_principal)],
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
) -> AsyncIterator[asyncpg.Connection]:
    """A connection inside a transaction that names the actor to the audit trigger.

    ``set_config(..., true)`` is transaction-local, so the actor cannot leak onto
    the next request that borrows this pooled connection. Every write endpoint
    should depend on this rather than on `get_connection`, otherwise the audit
    row is written with a NULL actor and the trail cannot answer "who".
    """
    async with connection.transaction():
        await connection.execute(
            "SELECT set_config('app.actor_id', $1, true), set_config('app.actor_label', $2, true)",
            principal.id if not principal.is_service else "",
            principal.label,
        )
        yield connection
