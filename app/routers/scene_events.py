"""Read access to scene/event findings from services/vlm_agent's Tier B.

Same shape as app/routers/detections.py's read path: `scene_events` is
written by workers/scene_event_worker.py and otherwise unread without this
module. A finding here is not a per-object detection - it is a judgement over
a camera's trigger window (loitering, wrong-way, collision, crowd density, or
an open-vocabulary "unusual activity"), always carrying a rationale an
operator can read alongside the source clip.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

import asyncpg
from fastapi import APIRouter, Depends, Query

from app.auth.dependencies import Principal, get_current_principal
from app.database import get_connection
from app.schemas import SceneEventListResponse, SceneEventRead

router = APIRouter(prefix="/api/v1/scene-events", tags=["scene-events"])

# Joined to cameras so a result is readable without a second request, the same
# reasoning app/routers/detections.py's _DETECTION_COLUMNS documents.
_SCENE_EVENT_COLUMNS: Final = """
        s.scene_event_id,
        s.camera_id,
        c.global_camera_code,
        c.site_name,
        s.window_start_utc_ms,
        s.window_end_utc_ms,
        s.event_type,
        s.confidence,
        s.rationale,
        s.implicated_target_ids,
        s.clip_uri,
        s.model_version,
        ST_Y(s.scene_event_geom) AS latitude,
        ST_X(s.scene_event_geom) AS longitude,
        s.workflow_state,
        s.ingested_at
"""


def _project(record: asyncpg.Record) -> SceneEventRead:
    row = dict(record)
    row["implicated_target_ids"] = list(row.get("implicated_target_ids") or [])
    return SceneEventRead.model_validate(row)


def _scope_predicate(principal: Principal, params: list[Any]) -> list[str]:
    """Restrict to the caller's department, resolved through the camera.

    scene_events carries no department column of its own - see the migration's
    comment on why - so scope is always resolved via the camera it came from.
    """
    scope = principal.department_scope
    if scope is None:
        return []
    params.append(scope)
    return [f"c.department_id = ${len(params)}"]


@router.get(
    "",
    response_model=SceneEventListResponse,
    summary="Search scene/event findings by camera, type or time",
)
async def list_scene_events(
    principal: Annotated[Principal, Depends(get_current_principal)],
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    camera_id: Annotated[str | None, Query()] = None,
    event_type: Annotated[str | None, Query(max_length=32)] = None,
    workflow_state: Annotated[str | None, Query(max_length=20)] = None,
    since_utc_ms: Annotated[int | None, Query(ge=0)] = None,
    until_utc_ms: Annotated[int | None, Query(ge=0)] = None,
    min_confidence: Annotated[float | None, Query(ge=0.0, le=1.0)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SceneEventListResponse:
    """Filtered scene event search, most recent window first."""
    params: list[Any] = []
    predicates: list[str] = _scope_predicate(principal, params)

    if camera_id:
        params.append(camera_id)
        predicates.append(f"s.camera_id = ${len(params)}::uuid")

    if event_type:
        params.append(event_type.upper())
        predicates.append(f"s.event_type = ${len(params)}")

    if workflow_state:
        params.append(workflow_state.upper())
        predicates.append(f"s.workflow_state = ${len(params)}")

    if since_utc_ms is not None:
        params.append(since_utc_ms)
        predicates.append(f"s.window_start_utc_ms >= ${len(params)}")

    if until_utc_ms is not None:
        params.append(until_utc_ms)
        predicates.append(f"s.window_start_utc_ms <= ${len(params)}")

    if min_confidence is not None:
        params.append(min_confidence)
        predicates.append(f"s.confidence >= ${len(params)}")

    where = f"WHERE {' AND '.join(predicates)}" if predicates else ""

    total = await connection.fetchval(
        f"""
        SELECT count(*)
        FROM scene_events s
        LEFT JOIN cameras c ON c.id = s.camera_id
        {where}
        """,
        *params,
    )

    params.extend([limit, offset])
    records = await connection.fetch(
        f"""
        SELECT {_SCENE_EVENT_COLUMNS}
        FROM scene_events s
        LEFT JOIN cameras c ON c.id = s.camera_id
        {where}
        ORDER BY s.window_start_utc_ms DESC
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
        """,
        *params,
    )

    return SceneEventListResponse(
        items=[_project(record) for record in records],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )
