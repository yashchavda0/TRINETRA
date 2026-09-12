"""Watchlist management: vehicles and identities of interest.

The worker holds this table in memory and refreshes it every few seconds, so a
vehicle added here starts matching almost immediately — no restart, no deploy.

Two liveness conditions apply and both are honoured on read: `is_active`, and
`expires_at`. Nothing sweeps expired rows, so an expired entry stays in the
table and is filtered out rather than deleted. That is deliberate: an expired
watchlist entry is still evidence of what was being looked for and when.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Final
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth.dependencies import (
    Principal,
    get_audited_connection,
    get_current_principal,
    require_role,
)
from app.database import get_connection
from app.schemas import (
    WatchlistCreate,
    WatchlistListResponse,
    WatchlistRead,
    WatchlistUpdate,
)

logger: Final = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/watchlist", tags=["watchlist"])

_COLUMNS: Final = (
    "id, plate_number, target_id, classification, priority, reason, "
    "case_reference, department_id, added_by, is_active, expires_at, created_at"
)


@router.get("", response_model=WatchlistListResponse, summary="List watchlist entries")
async def list_watchlist(
    principal: Annotated[Principal, Depends(get_current_principal)],
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    include_inactive: Annotated[bool, Query()] = False,
    plate: Annotated[str | None, Query(max_length=24)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> WatchlistListResponse:
    """Entries visible to the caller's department."""
    params: list[Any] = []
    predicates: list[str] = []

    scope = principal.department_scope
    if scope is not None:
        params.append(scope)
        # A statewide entry (no department) is visible to everyone: a stolen
        # vehicle does not stop being stolen at a department boundary.
        predicates.append(f"(department_id = ${len(params)} OR department_id IS NULL)")

    if not include_inactive:
        predicates.append("is_active")
        predicates.append("(expires_at IS NULL OR expires_at > clock_timestamp())")

    if plate:
        params.append(plate.upper().replace(" ", ""))
        predicates.append(f"plate_number = ${len(params)}")

    where = f"WHERE {' AND '.join(predicates)}" if predicates else ""

    total = await connection.fetchval(f"SELECT count(*) FROM watchlist {where}", *params)

    params.append(limit)
    records = await connection.fetch(
        f"""
        SELECT {_COLUMNS} FROM watchlist {where}
        ORDER BY priority, created_at DESC
        LIMIT ${len(params)}
        """,
        *params,
    )

    return WatchlistListResponse(
        items=[WatchlistRead.model_validate(dict(r)) for r in records],
        total=int(total or 0),
    )


@router.post(
    "",
    response_model=WatchlistRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a vehicle or identity of interest",
)
async def create_entry(
    payload: WatchlistCreate,
    principal: Annotated[Principal, Depends(require_role("OPERATOR"))],
    connection: Annotated[asyncpg.Connection, Depends(get_audited_connection)],
) -> WatchlistRead:
    """Add an entry, owned by the caller's department."""
    try:
        record = await connection.fetchrow(
            f"""
            INSERT INTO watchlist (
                plate_number, target_id, classification, priority, reason,
                case_reference, department_id, added_by, expires_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING {_COLUMNS}
            """,
            payload.plate_number,
            payload.target_id,
            payload.classification.upper(),
            payload.priority,
            payload.reason,
            payload.case_reference,
            principal.department_scope,
            None if principal.is_service else UUID(principal.id),
            payload.expires_at,
        )
    except asyncpg.UniqueViolationError as exc:
        # idx_watchlist_plate_active permits one ACTIVE entry per plate.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"'{payload.plate_number}' is already on the active watchlist. "
                "Deactivate the existing entry before adding a new one."
            ),
        ) from exc
    except asyncpg.CheckViolationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"entry rejected by a database constraint: {exc.constraint_name}",
        ) from exc

    logger.warning(
        "watchlist entry added",
        extra={
            "plate": payload.plate_number,
            "target_id": payload.target_id,
            "priority": payload.priority,
            "actor": principal.label,
        },
    )
    return WatchlistRead.model_validate(dict(record))


@router.patch(
    "/{entry_id:uuid}",
    response_model=WatchlistRead,
    summary="Update or deactivate a watchlist entry",
)
async def update_entry(
    entry_id: UUID,
    payload: WatchlistUpdate,
    principal: Annotated[Principal, Depends(require_role("OPERATOR"))],
    connection: Annotated[asyncpg.Connection, Depends(get_audited_connection)],
) -> WatchlistRead:
    """Partial update. Deactivating is how an entry is retired."""
    owner = await connection.fetchrow(
        "SELECT department_id FROM watchlist WHERE id = $1", entry_id
    )
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="watchlist entry not found"
        )
    principal.assert_can_access_department(owner["department_id"])

    assignments: list[str] = []
    params: list[Any] = []

    for column, value in (
        ("classification", payload.classification.upper() if payload.classification else None),
        ("priority", payload.priority),
        ("reason", payload.reason),
        ("case_reference", payload.case_reference),
        ("is_active", payload.is_active),
        ("expires_at", payload.expires_at),
    ):
        if value is not None:
            params.append(value)
            assignments.append(f"{column} = ${len(params)}")

    if not assignments:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="no updatable fields supplied",
        )

    params.append(entry_id)
    record = await connection.fetchrow(
        f"""
        UPDATE watchlist SET {', '.join(assignments)}
        WHERE id = ${len(params)}
        RETURNING {_COLUMNS}
        """,
        *params,
    )

    logger.info(
        "watchlist entry updated",
        extra={
            "entry_id": str(entry_id),
            "fields": sorted(payload.model_fields_set),
            "actor": principal.label,
        },
    )
    return WatchlistRead.model_validate(dict(record))


@router.delete(
    "/{entry_id:uuid}",
    response_model=WatchlistRead,
    summary="Retire a watchlist entry",
)
async def retire_entry(
    entry_id: UUID,
    principal: Annotated[Principal, Depends(require_role("OPERATOR"))],
    connection: Annotated[asyncpg.Connection, Depends(get_audited_connection)],
) -> WatchlistRead:
    """Soft delete: deactivated, never removed.

    Why a watchlist entry was created, by whom, and when it stopped applying are
    all facts a later investigation may need. Deleting the row destroys them.
    """
    owner = await connection.fetchrow(
        "SELECT department_id FROM watchlist WHERE id = $1", entry_id
    )
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="watchlist entry not found"
        )
    principal.assert_can_access_department(owner["department_id"])

    record = await connection.fetchrow(
        f"UPDATE watchlist SET is_active = FALSE WHERE id = $1 RETURNING {_COLUMNS}",
        entry_id,
    )
    logger.info(
        "watchlist entry retired",
        extra={"entry_id": str(entry_id), "actor": principal.label},
    )
    return WatchlistRead.model_validate(dict(record))
