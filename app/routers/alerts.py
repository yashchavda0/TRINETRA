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
import secrets
from typing import Annotated, Any, Final
from uuid import UUID

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

from app.auth.dependencies import Principal, get_current_principal, require_role
from app.config import Settings, get_settings
from app.database import get_connection
from app.schemas import (
    AlertListResponse,
    AlertPublishRequest,
    AlertPublishResult,
    AlertRead,
    AlertWorkflowUpdate,
)

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

    An unconfigured key closes the channel rather than opening it. The previous
    behaviour - return early, allow everything - meant that forgetting one
    environment variable silently published the P0 feed, and that feed carries
    `subject` data the schema flags as identity information from an audited
    CCTNS lookup. A missing secret must fail shut.
    """
    expected = settings.p0_alert_api_key
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "the alert channel has no key configured; set P0_ALERT_API_KEY "
                "and restart the API"
            ),
        )

    presented = api_key_header or token
    if not presented and authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            presented = value.strip()

    # compare_digest rather than !=: a plain comparison returns as soon as two
    # bytes differ, and that timing difference is enough to recover a shared
    # secret one character at a time.
    if not presented or not secrets.compare_digest(presented, expected):
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


# ---------------------------------------------------------------------------
# Alert history and workflow
# ---------------------------------------------------------------------------

_ALERT_COLUMNS: Final = """
        a.alert_id,
        a.priority,
        a.classification,
        a.plate_number,
        a.camera_id,
        c.global_camera_code,
        c.site_name,
        ST_Y(a.alert_geom) AS latitude,
        ST_X(a.alert_geom) AS longitude,
        a.detected_at_utc_ms,
        a.dispatched_at_utc_ms,
        a.confidence,
        a.vehicle,
        a.subject,
        a.evidence,
        a.workflow_state,
        a.acknowledged_by,
        a.acknowledged_at,
        a.assigned_to,
        a.resolution_note,
        a.received_at
"""


def _project_alert(record: asyncpg.Record, principal: Principal) -> AlertRead:
    """Build the response, withholding identity data from lesser roles.

    `subject` carries the named record behind an audited CCTNS request, which
    sql/002 flags as SELECT-restricted. An operator sees the vehicle and the
    classification - everything needed to act - without the person's identity.
    """
    row = dict(record)
    for key in ("vehicle", "subject", "evidence"):
        value = row.get(key)
        if isinstance(value, str):
            try:
                row[key] = json.loads(value)
            except ValueError:
                row[key] = {}
        elif value is None:
            row[key] = {}

    if not principal.at_least("DEPT_ADMIN"):
        row["subject"] = {"restricted": True}

    return AlertRead.model_validate(row)


@router.get("", response_model=AlertListResponse, summary="Alert history")
async def list_alerts(
    principal: Annotated[Principal, Depends(get_current_principal)],
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    workflow_state: Annotated[str | None, Query(max_length=20)] = None,
    priority: Annotated[str | None, Query(max_length=4)] = None,
    plate: Annotated[str | None, Query(max_length=24)] = None,
    camera_id: Annotated[str | None, Query()] = None,
    since_utc_ms: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AlertListResponse:
    """Recorded alerts, newest first.

    Until now `threat_alerts` was written and never read: an operator who
    connected to the live socket a second after an alert fired had no way to
    recover it. This is that way.
    """
    params: list[Any] = []
    predicates: list[str] = []

    scope = principal.department_scope
    if scope is not None:
        params.append(scope)
        # Matched through the camera: threat_alerts has no department column of
        # its own, and an alert belongs to whoever owns the camera that raised it.
        predicates.append(f"c.department_id = ${len(params)}")

    if workflow_state:
        params.append(workflow_state.upper())
        predicates.append(f"a.workflow_state = ${len(params)}")

    if priority:
        params.append(priority.upper())
        predicates.append(f"a.priority = ${len(params)}")

    if plate:
        params.append(plate.upper().replace(" ", ""))
        predicates.append(f"a.plate_number = ${len(params)}")

    if camera_id:
        params.append(camera_id)
        predicates.append(f"a.camera_id = ${len(params)}::uuid")

    if since_utc_ms is not None:
        params.append(since_utc_ms)
        predicates.append(f"a.detected_at_utc_ms >= ${len(params)}")

    where = f"WHERE {' AND '.join(predicates)}" if predicates else ""

    total = await connection.fetchval(
        f"""
        SELECT count(*) FROM threat_alerts a
        LEFT JOIN cameras c ON c.id = a.camera_id
        {where}
        """,
        *params,
    )

    params.extend([limit, offset])
    records = await connection.fetch(
        f"""
        SELECT {_ALERT_COLUMNS}
        FROM threat_alerts a
        LEFT JOIN cameras c ON c.id = a.camera_id
        {where}
        ORDER BY a.detected_at_utc_ms DESC
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
        """,
        *params,
    )

    return AlertListResponse(
        items=[_project_alert(record, principal) for record in records],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/{alert_id}",
    response_model=AlertRead,
    summary="Acknowledge, assign or resolve an alert",
)
async def update_alert(
    alert_id: str,
    payload: AlertWorkflowUpdate,
    principal: Annotated[Principal, Depends(require_role("OPERATOR"))],
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
) -> AlertRead:
    """Move an alert through its workflow.

    Acknowledgement records who and when, so "someone is on this" is a fact
    rather than an assumption.
    """
    existing = await connection.fetchrow(
        """
        SELECT a.alert_id, c.department_id
        FROM threat_alerts a
        LEFT JOIN cameras c ON c.id = a.camera_id
        WHERE a.alert_id = $1
        """,
        alert_id,
    )
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"alert '{alert_id}' not found"
        )
    principal.assert_can_access_department(existing["department_id"])

    assignments: list[str] = []
    params: list[Any] = []

    if payload.workflow_state is not None:
        params.append(payload.workflow_state)
        assignments.append(f"workflow_state = ${len(params)}")
        if payload.workflow_state == "ACKNOWLEDGED":
            params.append(None if principal.is_service else UUID(principal.id))
            assignments.append(f"acknowledged_by = ${len(params)}")
            assignments.append("acknowledged_at = clock_timestamp()")

    if payload.assigned_to is not None:
        params.append(payload.assigned_to)
        assignments.append(f"assigned_to = ${len(params)}")

    if payload.resolution_note is not None:
        params.append(payload.resolution_note)
        assignments.append(f"resolution_note = ${len(params)}")

    params.append(alert_id)
    record = await connection.fetchrow(
        f"""
        WITH updated AS (
            UPDATE threat_alerts SET {', '.join(assignments)}
            WHERE alert_id = ${len(params)}
            RETURNING *
        )
        SELECT {_ALERT_COLUMNS}
        FROM updated a
        LEFT JOIN cameras c ON c.id = a.camera_id
        """,
        *params,
    )

    logger.info(
        "alert workflow updated",
        extra={
            "alert_id": alert_id,
            "workflow_state": payload.workflow_state,
            "actor": principal.label,
        },
    )
    return _project_alert(record, principal)


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
