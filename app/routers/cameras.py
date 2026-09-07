"""Model 1 Camera Registry endpoints.

Axis-order rule enforced in this module: incoming payloads carry
``latitude``/``longitude`` in that order, PostGIS wants
``ST_MakePoint(longitude, latitude)``. Every bind list below therefore places
longitude before latitude. Getting this backwards silently returns cameras from
the wrong district, so the order is asserted by the regression test described in
the project verification steps.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Final
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.config import Settings, get_settings
from app.database import get_connection
from app.schemas import (
    CameraCreate,
    CameraHealthRead,
    CameraListResponse,
    CameraRead,
    HealthPingBatch,
    HealthPingResult,
    SpatialSearchQuery,
)

logger: Final = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/cameras", tags=["cameras"])

# Column projection shared by every read path. location_geom is never sent to
# the client as WKB: it is decomposed into scalar degrees here.
#   ST_X -> longitude, ST_Y -> latitude
_CAMERA_COLUMNS: Final = """
        id,
        global_camera_code,
        department_id,
        ST_Y(location_geom) AS latitude,
        ST_X(location_geom) AS longitude,
        azimuth_angle,
        fov_degrees,
        stream_url,
        vms_vendor,
        status,
        created_at
"""

_INSERT_CAMERA_SQL: Final = f"""
    INSERT INTO cameras (
        global_camera_code,
        department_id,
        location_geom,
        azimuth_angle,
        fov_degrees,
        stream_url,
        vms_vendor,
        status
    )
    VALUES (
        $1,
        $2,
        ST_SetSRID(ST_MakePoint($3, $4), 4326),
        $5,
        $6,
        $7,
        $8,
        $9
    )
    RETURNING {_CAMERA_COLUMNS}
"""

_INSERT_HEALTH_LOG_SQL: Final = """
    INSERT INTO camera_health_logs (camera_id, ping_latency_ms, is_reachable)
    VALUES ($1, $2, $3)
"""


def spatial_search_params(
    latitude: Annotated[
        float,
        Query(ge=-90.0, le=90.0, description="WGS84 latitude of the search centre"),
    ],
    longitude: Annotated[
        float,
        Query(ge=-180.0, le=180.0, description="WGS84 longitude of the search centre"),
    ],
    radius_meters: Annotated[
        float,
        Query(gt=0.0, le=50_000.0, description="Search radius in true metres"),
    ],
    limit: Annotated[
        int, Query(ge=1, le=10_000, description="Maximum cameras to return")
    ] = 100,
    department_id: Annotated[
        str | None, Query(max_length=32, description="Restrict to one department code")
    ] = None,
    status_filter: Annotated[
        str | None,
        Query(
            alias="status",
            description="Restrict to one camera status: ACTIVE, INACTIVE, MAINTENANCE, DECOMMISSIONED",
        ),
    ] = None,
) -> SpatialSearchQuery:
    """Validate radius-search query parameters into a single model.

    FastAPI validates the primitives first, so a bad value produces a 422 rather
    than an internal error, and the model construction below is guaranteed to
    succeed for the ranges already checked.
    """
    try:
        return SpatialSearchQuery(
            latitude=latitude,
            longitude=longitude,
            radius_meters=radius_meters,
            limit=limit,
            department_id=department_id,
            status=status_filter,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.post(
    "",
    response_model=CameraRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register a camera in the Model 1 registry",
)
async def create_camera(
    payload: CameraCreate,
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
) -> CameraRead:
    """Ingest camera metadata and store its position as an EPSG:4326 point."""
    try:
        record = await connection.fetchrow(
            _INSERT_CAMERA_SQL,
            payload.global_camera_code,
            payload.department_id,
            payload.longitude,  # $3 -> ST_MakePoint X
            payload.latitude,  # $4 -> ST_MakePoint Y
            payload.azimuth_angle,
            payload.fov_degrees,
            payload.stream_url,
            payload.vms_vendor,
            payload.status,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"camera with global_camera_code '{payload.global_camera_code}' "
                "is already registered"
            ),
        ) from exc
    except asyncpg.ForeignKeyViolationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown department_id '{payload.department_id}'",
        ) from exc
    except asyncpg.CheckViolationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"camera metadata rejected by a database constraint: {exc.constraint_name}",
        ) from exc

    camera = CameraRead.model_validate(dict(record))
    logger.info(
        "camera registered",
        extra={
            "camera_id": str(camera.id),
            "global_camera_code": camera.global_camera_code,
            "department_id": camera.department_id,
        },
    )
    return camera


@router.get(
    "",
    response_model=CameraListResponse,
    summary="List registered cameras",
)
async def list_cameras(
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: Annotated[int, Query(ge=1, le=100_000)] = 1_000,
    offset: Annotated[int, Query(ge=0)] = 0,
    department_id: Annotated[str | None, Query(max_length=32)] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> CameraListResponse:
    """Fleet listing used by the GIS console to draw every camera on load."""
    effective_limit = min(limit, settings.max_list_results)

    params: list[Any] = []
    predicates: list[str] = []

    if department_id is not None:
        params.append(department_id.upper())
        predicates.append(f"department_id = ${len(params)}")

    if status_filter is not None:
        params.append(status_filter.upper())
        predicates.append(f"status = ${len(params)}")

    where_clause = f"WHERE {' AND '.join(predicates)}" if predicates else ""

    total = await connection.fetchval(
        f"SELECT count(*) FROM cameras {where_clause}", *params
    )

    params.extend([effective_limit, offset])
    records = await connection.fetch(
        f"""
        SELECT {_CAMERA_COLUMNS}
        FROM cameras
        {where_clause}
        ORDER BY global_camera_code
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
        """,
        *params,
    )

    return CameraListResponse(
        items=[CameraRead.model_validate(dict(record)) for record in records],
        total=int(total or 0),
        limit=effective_limit,
        offset=offset,
    )


@router.get(
    "/{camera_id}/health",
    response_model=CameraHealthRead,
    summary="Latest reachability state of one camera",
)
async def camera_health(
    camera_id: UUID,
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
) -> CameraHealthRead:
    """Most recent `camera_health_logs` row for a camera.

    Served from the `(camera_id, logged_at DESC)` index, so it stays a single
    index lookup no matter how deep the telemetry history grows.
    """
    exists = await connection.fetchval("SELECT 1 FROM cameras WHERE id = $1", camera_id)
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"camera '{camera_id}' is not registered",
        )

    record = await connection.fetchrow(
        """
        SELECT is_reachable, ping_latency_ms, logged_at
        FROM camera_health_logs
        WHERE camera_id = $1
        ORDER BY logged_at DESC
        LIMIT 1
        """,
        camera_id,
    )

    # A registered camera that has never been polled is UNKNOWN, not DOWN:
    # reporting DOWN would make a fresh registration look like an outage.
    if record is None:
        return CameraHealthRead(camera_id=camera_id, status="UNKNOWN")

    return CameraHealthRead(
        camera_id=camera_id,
        status="UP" if record["is_reachable"] else "DOWN",
        is_reachable=record["is_reachable"],
        ping_latency_ms=record["ping_latency_ms"],
        last_ping_at=record["logged_at"],
    )


@router.get(
    "/spatial-search",
    response_model=list[CameraRead],
    summary="Find cameras within a metre-accurate radius of a point",
)
async def spatial_search(
    query: Annotated[SpatialSearchQuery, Depends(spatial_search_params)],
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[CameraRead]:
    """Radius search using ``ST_DWithin`` on the geography cast.

    The geography cast makes ``radius_meters`` true metres at every latitude,
    and the ``idx_cameras_location_geog`` GIST index on that same cast keeps the
    query index-assisted at fleet scale.
    """
    effective_limit = min(query.limit, settings.max_spatial_results)

    # $1 longitude, $2 latitude, $3 radius. Optional filters take $4 onward so
    # the numbering stays stable whichever combination is supplied.
    params: list[Any] = [query.longitude, query.latitude, query.radius_meters]
    predicates: list[str] = [
        "ST_DWithin("
        "    location_geom::geography,"
        "    ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography,"
        "    $3"
        ")"
    ]

    if query.department_id is not None:
        params.append(query.department_id)
        predicates.append(f"department_id = ${len(params)}")

    if query.status is not None:
        params.append(query.status)
        predicates.append(f"status = ${len(params)}")

    params.append(effective_limit)
    limit_placeholder = f"${len(params)}"

    sql = f"""
        SELECT
            {_CAMERA_COLUMNS},
            ST_Distance(
                location_geom::geography,
                ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
            ) AS distance_meters
        FROM cameras
        WHERE {' AND '.join(predicates)}
        ORDER BY distance_meters ASC
        LIMIT {limit_placeholder}
    """

    records = await connection.fetch(sql, *params)

    logger.info(
        "spatial search executed",
        extra={
            "latitude": query.latitude,
            "longitude": query.longitude,
            "radius_meters": query.radius_meters,
            "result_count": len(records),
        },
    )
    return [CameraRead.model_validate(dict(record)) for record in records]


@router.post(
    "/health-ping",
    response_model=HealthPingResult,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Bulk-record camera reachability checks",
)
async def health_ping(
    payload: HealthPingBatch,
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HealthPingResult:
    """Insert a batch of health checks into ``camera_health_logs`` atomically."""
    if len(payload.pings) > settings.max_health_ping_batch:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"batch of {len(payload.pings)} exceeds the configured maximum of "
                f"{settings.max_health_ping_batch} pings per request"
            ),
        )

    submitted_ids: list[UUID] = [ping.camera_id for ping in payload.pings]
    rows = [
        (ping.camera_id, ping.ping_latency_ms, ping.is_reachable)
        for ping in payload.pings
    ]

    async with connection.transaction():
        # Resolve unknown camera ids up front so the client is told exactly which
        # ids are unregistered, instead of receiving an opaque FK violation.
        known = await connection.fetch(
            "SELECT id FROM cameras WHERE id = ANY($1::uuid[])",
            list(set(submitted_ids)),
        )
        known_ids = {record["id"] for record in known}
        unknown_ids = [
            str(camera_id) for camera_id in dict.fromkeys(submitted_ids)
            if camera_id not in known_ids
        ]
        if unknown_ids:
            preview = ", ".join(unknown_ids[:10])
            suffix = "" if len(unknown_ids) <= 10 else f" (and {len(unknown_ids) - 10} more)"
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"{len(unknown_ids)} camera_id value(s) are not registered: "
                    f"{preview}{suffix}"
                ),
            )

        await connection.executemany(_INSERT_HEALTH_LOG_SQL, rows)

    unreachable = sum(1 for ping in payload.pings if not ping.is_reachable)
    logger.info(
        "health pings recorded",
        extra={
            "inserted": len(rows),
            "unreachable_count": unreachable,
            "distinct_cameras": len(set(submitted_ids)),
        },
    )
    return HealthPingResult(inserted=len(rows))
