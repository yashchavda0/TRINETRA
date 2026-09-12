"""Read access to the metadata audit trail.

The trail itself is written by a database trigger (migration 003), so this
router only reads. There is deliberately no write endpoint and no delete: an
audit log the application can edit is not evidence.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Final
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Query

from app.auth.dependencies import Principal, require_role
from app.database import get_connection
from app.schemas import AuditEntry, AuditListResponse

logger: Final = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


def _as_dict(value: Any) -> dict[str, Any] | None:
    """asyncpg hands JSONB back as text; the API returns it as an object."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except (TypeError, ValueError):
        return None


@router.get(
    "",
    response_model=AuditListResponse,
    summary="Search the metadata audit trail",
)
async def list_audit(
    principal: Annotated[Principal, Depends(require_role("DEPT_ADMIN"))],
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    entity: Annotated[str | None, Query(max_length=40)] = None,
    entity_id: Annotated[str | None, Query(max_length=64)] = None,
    action: Annotated[str | None, Query(pattern="^(INSERT|UPDATE|DELETE)$")] = None,
    actor_id: Annotated[UUID | None, Query()] = None,
) -> AuditListResponse:
    """Filterable audit history, newest first."""
    params: list[Any] = []
    predicates: list[str] = []

    # A department admin sees changes to their own department's cameras and to
    # their own users - not the whole state's history.
    scope = principal.department_scope
    if scope is not None:
        params.append(scope)
        predicates.append(
            f"""(
                (a.entity = 'cameras' AND COALESCE(a.after ->> 'department_id', a.before ->> 'department_id') = ${len(params)})
                OR (a.entity = 'users' AND COALESCE(a.after ->> 'department_id', a.before ->> 'department_id') = ${len(params)})
            )"""
        )

    for column, value in (("entity", entity), ("entity_id", entity_id), ("action", action)):
        if value:
            params.append(value)
            predicates.append(f"a.{column} = ${len(params)}")

    if actor_id:
        params.append(actor_id)
        predicates.append(f"a.actor_id = ${len(params)}")

    where = f"WHERE {' AND '.join(predicates)}" if predicates else ""

    total = await connection.fetchval(f"SELECT count(*) FROM audit_log a {where}", *params)

    params.extend([limit, offset])
    records = await connection.fetch(
        f"""
        SELECT a.id, a.entity, a.entity_id, a.action, a.actor_id, a.actor_label,
               a.changed, a.before, a.after, a.at
        FROM audit_log a
        {where}
        ORDER BY a.at DESC, a.id DESC
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
        """,
        *params,
    )

    return AuditListResponse(
        items=[
            AuditEntry(
                id=row["id"],
                entity=row["entity"],
                entity_id=row["entity_id"],
                action=row["action"],
                actor_id=row["actor_id"],
                actor_label=row["actor_label"],
                changed=list(row["changed"] or []),
                before=_as_dict(row["before"]),
                after=_as_dict(row["after"]),
                at=row["at"],
            )
            for row in records
        ],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )
