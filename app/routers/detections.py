"""Read access to detections and vehicle movement history.

`detections` has been written by the worker since migration 002 and read by
nothing: a well-indexed, entirely write-only table. This module is the read path
those indexes were built for, and it is what makes the tender's Model 2
"searchable vehicle-movement records" real.

What a movement history is and is not: it is the ordered list of cameras that
read a plate, with the gaps between them. It is not a tracked path. A vehicle
that leaves the camera network and returns produces two sightings with a long
gap, and nothing here claims to know what happened in between. The response says
so in its own `caveat` field rather than leaving an operator to infer it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import uuid
from pathlib import Path
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
from fastapi.responses import FileResponse

from app.auth.dependencies import Principal, get_current_principal
from app.config import Settings, get_settings
from app.database import get_connection
from app.routers.alerts import _authorise
from app.schemas import (
    DetectionListResponse,
    DetectionPublishRequest,
    DetectionPublishResult,
    DetectionRead,
    MovementHistory,
    MovementHop,
)

logger: Final = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/detections", tags=["detections"])
ws_router = APIRouter(tags=["detections"])

# Connected consoles watching the live feed. Same shape as the alert
# subscriber set in app/routers/alerts.py, kept separate because the two
# channels have independent, unrelated subscriber lifecycles.
_subscribers: set[WebSocket] = set()
_subscribers_lock: Final = asyncio.Lock()

# Joined to cameras so a result is readable without a second request: an
# operator searching a plate wants "Ashram Road North", not a UUID.
_DETECTION_COLUMNS: Final = """
        d.event_id,
        d.camera_id,
        c.global_camera_code,
        c.site_name,
        d.department_id,
        d.timestamp_utc_ms,
        d.object_class,
        d.plate_number,
        d.plate_confidence,
        d.track_id,
        d.target_id,
        ST_Y(d.detection_geom) AS latitude,
        ST_X(d.detection_geom) AS longitude,
        d.embedding_accepted,
        d.snapshot_uri,
        d.bbox_x_min,
        d.bbox_y_min,
        d.bbox_x_max,
        d.bbox_y_max,
        d.attributes
"""

EARTH_RADIUS_M: Final = 6_371_008.8


def _project(record: asyncpg.Record) -> DetectionRead:
    row = dict(record)
    attributes = row.get("attributes")
    if isinstance(attributes, str):
        # asyncpg returns JSONB as text unless a codec is registered.
        try:
            row["attributes"] = json.loads(attributes)
        except ValueError:
            row["attributes"] = {}
    elif attributes is None:
        row["attributes"] = {}
    return DetectionRead.model_validate(row)


def _scope_predicate(principal: Principal, params: list[Any]) -> list[str]:
    """Restrict to the caller's department, from their token.

    `detections.department_id` is denormalised onto the row by the producer, but
    it can be NULL when a producer omitted the department code. Those rows are
    matched through the camera instead, so a detection is never invisible to the
    department that owns the camera that made it.
    """
    scope = principal.department_scope
    if scope is None:
        return []
    params.append(scope)
    placeholder = f"${len(params)}"
    return [f"(d.department_id = {placeholder} OR c.department_id = {placeholder})"]


@router.get(
    "",
    response_model=DetectionListResponse,
    summary="Search detections by plate, camera, time or class",
)
async def list_detections(
    principal: Annotated[Principal, Depends(get_current_principal)],
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    plate: Annotated[str | None, Query(max_length=24, description="Exact plate")] = None,
    plate_like: Annotated[
        str | None, Query(max_length=24, description="Partial plate, for a misread digit")
    ] = None,
    camera_id: Annotated[str | None, Query()] = None,
    object_class: Annotated[str | None, Query(max_length=32)] = None,
    since_utc_ms: Annotated[int | None, Query(ge=0)] = None,
    until_utc_ms: Annotated[int | None, Query(ge=0)] = None,
    plates_only: Annotated[
        bool, Query(description="Only detections that carry a plate reading")
    ] = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DetectionListResponse:
    """Filtered detection search, newest first."""
    params: list[Any] = []
    predicates: list[str] = _scope_predicate(principal, params)

    if plate:
        # Normalised the same way the worker stores it, so a search for
        # "GJ 01 AB 1234" finds a row stored as "GJ01AB1234".
        params.append(_normalise(plate))
        predicates.append(f"d.plate_number = ${len(params)}")

    if plate_like:
        params.append(f"%{_normalise(plate_like)}%")
        predicates.append(f"d.plate_number LIKE ${len(params)}")

    if camera_id:
        params.append(camera_id)
        predicates.append(f"d.camera_id = ${len(params)}::uuid")

    if object_class:
        params.append(object_class.upper())
        predicates.append(f"d.object_class = ${len(params)}")

    if since_utc_ms is not None:
        params.append(since_utc_ms)
        predicates.append(f"d.timestamp_utc_ms >= ${len(params)}")

    if until_utc_ms is not None:
        params.append(until_utc_ms)
        predicates.append(f"d.timestamp_utc_ms <= ${len(params)}")

    if plates_only or plate or plate_like:
        predicates.append("d.plate_number IS NOT NULL")

    where = f"WHERE {' AND '.join(predicates)}" if predicates else ""

    total = await connection.fetchval(
        f"""
        SELECT count(*)
        FROM detections d
        LEFT JOIN cameras c ON c.id = d.camera_id
        {where}
        """,
        *params,
    )

    params.extend([limit, offset])
    records = await connection.fetch(
        f"""
        SELECT {_DETECTION_COLUMNS}
        FROM detections d
        LEFT JOIN cameras c ON c.id = d.camera_id
        {where}
        ORDER BY d.timestamp_utc_ms DESC
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
        """,
        *params,
    )

    return DetectionListResponse(
        items=[_project(record) for record in records],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/plate/{plate}/movements",
    response_model=MovementHistory,
    summary="Every sighting of one plate, with the legs between them",
)
async def plate_movements(
    plate: str,
    principal: Annotated[Principal, Depends(get_current_principal)],
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    since_utc_ms: Annotated[int | None, Query(ge=0)] = None,
    until_utc_ms: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=2, le=1000)] = 200,
) -> MovementHistory:
    """Ordered sightings plus inter-camera transit times.

    Consecutive reads at the *same* camera are not a hop - a vehicle dwelling in
    view produces many of them - so those collapse into the single sighting that
    starts the dwell.
    """
    normalised = _normalise(plate)
    if not normalised:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="a plate number is required",
        )

    params: list[Any] = [normalised]
    predicates = [f"d.plate_number = $1"]
    predicates.extend(_scope_predicate(principal, params))

    if since_utc_ms is not None:
        params.append(since_utc_ms)
        predicates.append(f"d.timestamp_utc_ms >= ${len(params)}")
    if until_utc_ms is not None:
        params.append(until_utc_ms)
        predicates.append(f"d.timestamp_utc_ms <= ${len(params)}")

    params.append(limit)
    records = await connection.fetch(
        f"""
        SELECT {_DETECTION_COLUMNS}
        FROM detections d
        LEFT JOIN cameras c ON c.id = d.camera_id
        WHERE {' AND '.join(predicates)}
        ORDER BY d.timestamp_utc_ms ASC
        LIMIT ${len(params)}
        """,
        *params,
    )

    sightings = [_project(record) for record in records]
    if not sightings:
        return MovementHistory(
            plate_number=normalised,
            sighting_count=0,
            distinct_cameras=0,
        )

    hops: list[MovementHop] = []
    previous = sightings[0]
    for current in sightings[1:]:
        if current.camera_id == previous.camera_id:
            # Same camera: a continuing dwell, not a journey leg. Keep the
            # earlier sighting as the departure point.
            continue

        transit_seconds = (current.timestamp_utc_ms - previous.timestamp_utc_ms) / 1000.0
        distance = _haversine(previous, current)
        speed = None
        if distance is not None and transit_seconds > 0:
            speed = round((distance / transit_seconds) * 3.6, 1)

        hops.append(
            MovementHop(
                from_camera_id=previous.camera_id,
                from_camera_code=previous.global_camera_code,
                to_camera_id=current.camera_id,
                to_camera_code=current.global_camera_code,
                departed_utc_ms=previous.timestamp_utc_ms,
                arrived_utc_ms=current.timestamp_utc_ms,
                transit_seconds=round(transit_seconds, 2),
                distance_meters=round(distance, 1) if distance is not None else None,
                implied_speed_kmh=speed,
            )
        )
        previous = current

    return MovementHistory(
        plate_number=normalised,
        sighting_count=len(sightings),
        first_seen_utc_ms=sightings[0].timestamp_utc_ms,
        last_seen_utc_ms=sightings[-1].timestamp_utc_ms,
        distinct_cameras=len({s.camera_id for s in sightings}),
        sightings=sightings,
        hops=hops,
    )


@router.get(
    "/{event_id}/snapshot",
    summary="The ANPR plate-crop image captured for one detection",
)
async def detection_snapshot(
    event_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FileResponse:
    """Serve the stored plate-crop JPEG for one detection.

    This is a small crop around the read plate - what the ANPR reader actually
    saw, kept as evidence for the OCR result - not the full camera frame. For
    that, see the recorded clip: GET /api/v2/streams/{camera_id}/clip, which
    the console overlays this same detection's bbox onto at the matching
    instant.

    `snapshot_uri` is never trusted as a filesystem path: only its final path
    component is used, resolved under `anpr_snapshot_dir`, so a row could not
    be made to serve an arbitrary file even if it somehow held one.
    """
    params: list[Any] = [event_id]
    predicates = ["d.event_id = $1"]
    predicates.extend(_scope_predicate(principal, params))

    record = await connection.fetchrow(
        f"""
        SELECT d.snapshot_uri
        FROM detections d
        LEFT JOIN cameras c ON c.id = d.camera_id
        WHERE {' AND '.join(predicates)}
        """,
        *params,
    )
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"detection '{event_id}' not found in your scope",
        )
    if not record["snapshot_uri"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="this detection has no snapshot",
        )

    filename = Path(record["snapshot_uri"]).name
    file_path = Path(settings.anpr_snapshot_dir).resolve() / filename
    if not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="snapshot file is missing on disk",
        )

    return FileResponse(file_path, media_type="image/jpeg")


async def broadcast(frame: dict[str, Any]) -> int:
    """Send one detection frame to every subscriber; returns how many received it."""
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
        logger.info("dropped %d unreachable detection subscriber(s)", len(dead))

    return delivered


def subscriber_count() -> int:
    """Number of currently connected consoles, for /health and logs."""
    return len(_subscribers)


@router.post(
    "/publish",
    response_model=DetectionPublishResult,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Fan out a just-persisted detection to live consoles",
)
async def publish_detection(
    payload: DetectionPublishRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> DetectionPublishResult:
    """Push a detection that is already committed to the live feed.

    The producer (the ANPR service, or the handoff worker) writes the
    ``detections`` row itself and calls this only afterwards - the same
    persist-then-broadcast ordering as ``/alerts/publish``, so what a console
    shows is always already on the record and recoverable from
    ``GET /api/v1/detections`` if the socket frame is missed.

    Reuses the P0 alert channel's key: both endpoints exist to let a trusted
    backend process push into a live console feed, and provisioning a second
    shared secret for the same trust boundary would add an operational
    variable without adding any security.
    """
    _authorise(settings, api_key_header=x_api_key, authorization=authorization)

    frame = payload.model_dump(mode="json")
    delivered = await broadcast(frame)

    return DetectionPublishResult(
        event_id=payload.event_id,
        subscribers_notified=delivered,
    )


@ws_router.websocket("/detections/live")
async def detections_socket(
    websocket: WebSocket,
    token: Annotated[str | None, Query()] = None,
) -> None:
    """Live detection feed for consoles, e.g. the Vehicle Search screen.

    Every sampled frame that yields a plate reaches this socket within a
    second or two of being read, which is what makes "process the live feed
    every second" visible rather than only true in a database row nobody is
    watching.
    """
    settings = get_settings()
    try:
        _authorise(
            settings,
            api_key_header=websocket.headers.get("x-api-key"),
            authorization=websocket.headers.get("authorization"),
            token=token,
        )
    except HTTPException:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        logger.warning("rejected unauthorised detection subscriber")
        return

    await websocket.accept()
    async with _subscribers_lock:
        _subscribers.add(websocket)
    logger.info("detection subscriber connected", extra={"subscribers": len(_subscribers)})

    try:
        # Receive-only; this read exists purely to observe the disconnect.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("detection subscriber failed")
    finally:
        async with _subscribers_lock:
            _subscribers.discard(websocket)
        logger.info(
            "detection subscriber disconnected", extra={"subscribers": len(_subscribers)}
        )


def _normalise(plate: str) -> str:
    """Strip separators and upper-case, matching how the worker stores plates."""
    return re.sub(r"[\s\-.]", "", plate).upper()


def _haversine(a: DetectionRead, b: DetectionRead) -> float | None:
    """Straight-line metres between two sightings, when both have a position."""
    if None in (a.latitude, a.longitude, b.latitude, b.longitude):
        return None

    phi1, phi2 = math.radians(a.latitude), math.radians(b.latitude)
    d_phi = phi2 - phi1
    d_lambda = math.radians(b.longitude - a.longitude)
    h = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, h)))
