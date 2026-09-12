"""Authentication and user administration.

Login, refresh, self-profile and user CRUD. Every other router in the service
depends on the principal this one issues tokens for.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any, Final
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from jwt import InvalidTokenError

from app.auth.dependencies import (
    Principal,
    get_audited_connection,
    get_current_principal,
    require_role,
)
from app.auth.security import (
    REFRESH_TOKEN_TYPE,
    create_access_token,
    create_refresh_token,
    decode_token,
    dummy_password_hash,
    hash_password,
    verify_password,
)
from app.config import Settings, get_settings
from app.database import get_connection
from app.schemas import (
    LoginRequest,
    RefreshRequest,
    TokenResponse,
    UserCreate,
    UserListResponse,
    UserRead,
    UserUpdate,
)

logger: Final = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
admin_router = APIRouter(prefix="/api/v1/users", tags=["users"])

_USER_COLUMNS: Final = (
    "id, email, full_name, role, department_id, is_active, last_login_at, created_at"
)


def _invalid_credentials() -> HTTPException:
    # One message for "no such user" and "wrong password" alike: distinguishing
    # them turns the login form into an account-enumeration oracle.
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="incorrect email or password",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _token_response(record: asyncpg.Record, settings: Settings) -> TokenResponse:
    user = UserRead.model_validate(dict(record))
    return TokenResponse(
        access_token=create_access_token(
            str(user.id), user.role, user.department_id, settings
        ),
        refresh_token=create_refresh_token(str(user.id), settings),
        expires_in=settings.access_token_ttl_minutes * 60,
        user=user,
    )


@router.post("/login", response_model=TokenResponse, summary="Exchange credentials for tokens")
async def login(
    payload: LoginRequest,
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TokenResponse:
    """Verify credentials and issue an access/refresh pair."""
    record = await connection.fetchrow(
        f"SELECT {_USER_COLUMNS}, password_hash FROM users WHERE lower(email) = lower($1)",
        payload.email,
    )

    if record is None:
        # Spend the same time a real verification would. Without this, response
        # latency alone reveals whether an address is registered.
        await asyncio.to_thread(
            verify_password, payload.password, dummy_password_hash()
        )
        raise _invalid_credentials()

    valid = await asyncio.to_thread(
        verify_password, payload.password, record["password_hash"]
    )
    if not valid:
        logger.warning("failed login", extra={"email": payload.email})
        raise _invalid_credentials()

    if not record["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this account has been deactivated",
        )

    await connection.execute(
        "UPDATE users SET last_login_at = clock_timestamp() WHERE id = $1", record["id"]
    )

    logger.info(
        "login succeeded",
        extra={
            "user_id": str(record["id"]),
            "role": record["role"],
            "department_id": record["department_id"],
        },
    )
    return _token_response(record, settings)


@router.post("/refresh", response_model=TokenResponse, summary="Mint a new access token")
async def refresh(
    payload: RefreshRequest,
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TokenResponse:
    """Exchange a refresh token for a fresh pair.

    The user row is re-read here rather than trusted from the token, so a role
    change or a deactivation takes effect at the next refresh at the latest.
    """
    try:
        claims = decode_token(payload.refresh_token, REFRESH_TOKEN_TYPE, settings)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid refresh token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    record = await connection.fetchrow(
        f"SELECT {_USER_COLUMNS} FROM users WHERE id = $1", UUID(claims["sub"])
    )
    if record is None or not record["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="account is inactive or no longer exists",
        )

    return _token_response(record, settings)


@router.get("/me", response_model=UserRead, summary="Profile of the authenticated caller")
async def me(
    principal: Annotated[Principal, Depends(get_current_principal)],
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
) -> UserRead:
    if principal.is_service:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="an API key has no user profile",
        )

    record = await connection.fetchrow(
        f"SELECT {_USER_COLUMNS} FROM users WHERE id = $1", UUID(principal.id)
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    return UserRead.model_validate(dict(record))


# ---------------------------------------------------------------------------
# User administration
# ---------------------------------------------------------------------------


@admin_router.get("", response_model=UserListResponse, summary="List users")
async def list_users(
    principal: Annotated[Principal, Depends(require_role("DEPT_ADMIN"))],
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    include_inactive: Annotated[bool, Query()] = False,
) -> UserListResponse:
    """List users, restricted to the caller's own department unless super admin."""
    predicates: list[str] = []
    params: list[Any] = []

    scope = principal.department_scope
    if scope is not None:
        params.append(scope)
        predicates.append(f"department_id = ${len(params)}")

    if not include_inactive:
        predicates.append("is_active")

    where = f"WHERE {' AND '.join(predicates)}" if predicates else ""

    total = await connection.fetchval(f"SELECT count(*) FROM users {where}", *params)
    params.extend([limit, offset])
    records = await connection.fetch(
        f"""
        SELECT {_USER_COLUMNS} FROM users {where}
        ORDER BY full_name
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
        """,
        *params,
    )

    return UserListResponse(
        items=[UserRead.model_validate(dict(r)) for r in records],
        total=int(total or 0),
    )


@admin_router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user",
)
async def create_user(
    payload: UserCreate,
    principal: Annotated[Principal, Depends(require_role("DEPT_ADMIN"))],
    connection: Annotated[asyncpg.Connection, Depends(get_audited_connection)],
) -> UserRead:
    """Create a console user within the caller's authority."""
    # A department admin may not mint a super admin, nor a user in another
    # department: either would be a privilege escalation out of their scope.
    if payload.role == "SUPER_ADMIN" and not principal.is_super_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only a SUPER_ADMIN can create another SUPER_ADMIN",
        )
    principal.assert_can_access_department(payload.department_id)

    password_hash = await asyncio.to_thread(hash_password, payload.password)

    try:
        record = await connection.fetchrow(
            f"""
            INSERT INTO users (email, full_name, password_hash, role, department_id)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING {_USER_COLUMNS}
            """,
            payload.email,
            payload.full_name,
            password_hash,
            payload.role,
            payload.department_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a user with email '{payload.email}' already exists",
        ) from exc
    except asyncpg.ForeignKeyViolationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown department_id '{payload.department_id}'",
        ) from exc
    except asyncpg.CheckViolationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"user rejected by a database constraint: {exc.constraint_name}",
        ) from exc

    logger.info("user created", extra={"user_id": str(record["id"]), "role": payload.role})
    return UserRead.model_validate(dict(record))


@admin_router.patch("/{user_id}", response_model=UserRead, summary="Update a user")
async def update_user(
    user_id: UUID,
    payload: UserUpdate,
    principal: Annotated[Principal, Depends(require_role("DEPT_ADMIN"))],
    connection: Annotated[asyncpg.Connection, Depends(get_audited_connection)],
) -> UserRead:
    """Partial update, including password reset and deactivation."""
    existing = await connection.fetchrow(
        "SELECT id, role, department_id FROM users WHERE id = $1", user_id
    )
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    principal.assert_can_access_department(existing["department_id"])
    if existing["role"] == "SUPER_ADMIN" and not principal.is_super_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only a SUPER_ADMIN can modify another SUPER_ADMIN",
        )
    if payload.role == "SUPER_ADMIN" and not principal.is_super_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only a SUPER_ADMIN can grant the SUPER_ADMIN role",
        )
    if payload.department_id is not None:
        principal.assert_can_access_department(payload.department_id)

    # Refuse to deactivate the last active super admin, which would lock every
    # user-administration path in the product.
    if payload.is_active is False and existing["role"] == "SUPER_ADMIN":
        remaining = await connection.fetchval(
            "SELECT count(*) FROM users WHERE role = 'SUPER_ADMIN' AND is_active AND id <> $1",
            user_id,
        )
        if not remaining:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="cannot deactivate the last active SUPER_ADMIN",
            )

    assignments: list[str] = []
    params: list[Any] = []

    if payload.password is not None:
        params.append(await asyncio.to_thread(hash_password, payload.password))
        assignments.append(f"password_hash = ${len(params)}")

    for column, value in (
        ("full_name", payload.full_name),
        ("role", payload.role),
        ("department_id", payload.department_id),
        ("is_active", payload.is_active),
    ):
        if value is not None:
            params.append(value)
            assignments.append(f"{column} = ${len(params)}")

    if not assignments:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="no updatable fields supplied",
        )

    params.append(user_id)
    try:
        record = await connection.fetchrow(
            f"""
            UPDATE users SET {', '.join(assignments)}
            WHERE id = ${len(params)}
            RETURNING {_USER_COLUMNS}
            """,
            *params,
        )
    except asyncpg.CheckViolationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "update rejected by a database constraint "
                f"({exc.constraint_name}) - every role except SUPER_ADMIN needs a department"
            ),
        ) from exc

    return UserRead.model_validate(dict(record))


@admin_router.post(
    "/bootstrap",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create the first administrator (only on an empty user table)",
)
async def bootstrap_admin(
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> UserRead:
    """Create the initial SUPER_ADMIN from configuration.

    Unauthenticated by necessity - there is no one to authenticate as yet - and
    therefore refused the moment any user exists. Credentials come from the
    environment, never from the request body, so this endpoint cannot be used to
    choose a password remotely.
    """
    if not settings.bootstrap_admin_email or not settings.bootstrap_admin_password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "bootstrap is not configured: set BOOTSTRAP_ADMIN_EMAIL and "
                "BOOTSTRAP_ADMIN_PASSWORD, then restart the API"
            ),
        )

    existing = await connection.fetchval("SELECT count(*) FROM users")
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="bootstrap is only available while no users exist",
        )

    password_hash = await asyncio.to_thread(
        hash_password, settings.bootstrap_admin_password
    )
    record = await connection.fetchrow(
        f"""
        INSERT INTO users (email, full_name, password_hash, role, department_id)
        VALUES ($1, $2, $3, 'SUPER_ADMIN', NULL)
        RETURNING {_USER_COLUMNS}
        """,
        settings.bootstrap_admin_email.lower(),
        "Bootstrap Administrator",
        password_hash,
    )

    logger.warning(
        "bootstrap super admin created - remove the bootstrap credentials from "
        "the environment and change this password",
        extra={"email": settings.bootstrap_admin_email},
    )
    return UserRead.model_validate(dict(record))
