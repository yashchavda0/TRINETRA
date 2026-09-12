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

from app.auth.dependencies import (
    Principal,
    get_audited_connection,
    get_current_principal,
    require_role,
)
from app.config import Settings, get_settings
from app.database import get_connection
from app.schemas import (
    BoundingBoxQuery,
    CameraCreate,
    CameraHealthRead,
    CameraListResponse,
    CameraRead,
    CameraUpdate,
    HealthPingBatch,
    HealthPingResult,
    SpatialSearchQuery,
)

logger: Final = logging.getLogger(__name__)

# Authentication is applied at the router, not per route: a new endpoint added
# here is protected by default rather than by remembering to add a dependency.
# Before this, every camera route was anonymous - including the listing that
# returns stream_url, which 001 documents as a credential-bearing secret.
router = APIRouter(
    prefix="/api/v1/cameras",
    tags=["cameras"],
    dependencies=[Depends(get_current_principal)],
)


def _project_camera(record: asyncpg.Record, principal: Principal) -> CameraRead:
    """Build the response model, redacting the stream URL for lesser roles.

    An RTSP URL frequently embeds camera credentials. Operators and viewers can
    watch a camera - the WebRTC proxy resolves the URL server-side for them -
    without ever being handed the URL itself.
    """
    row = dict(record)
    if not principal.at_least("DEPT_ADMIN"):
        row["stream_url"] = "[redacted]"
    return CameraRead.model_validate(row)


def _scope_predicate(
    principal: Principal, params: list[Any], requested_department: str | None
) -> list[str]:
    """Return SQL predicates enforcing the caller's department scope.

    The caller's own scope is applied unconditionally; a requested department is
    an additional narrowing filter, never a way to widen. Omitting the filter
    must not expose another department's fleet.
    """
    predicates: list[str] = []

    scope = principal.department_scope
    if scope is not None:
        params.append(scope)
        predicates.append(f"department_id = ${len(params)}")
        if requested_department and requested_department.upper() != scope.upper():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"your account is scoped to department '{scope}'",
            )
    elif requested_department:
        params.append(requested_department.upper())
        predicates.append(f"department_id = ${len(params)}")

    return predicates

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
        created_at,
        camera_type,
        make,
        model,
        serial_number,
        ip_address,
        resolution,
        codec,
        frame_rate,
        installed_on,
        owner_org,
        custodian_name,
        custodian_contact,
        nvr_reference,
        nvr_channel,
        retention_days,
        recording_enabled,
        site_name,
        address,
        ward,
        zone,
        district,
        connectivity_status,
        connectivity_type,
        last_seen_at,
        maintenance_state,
        last_serviced_on,
        next_service_due,
        work_order_ref,
        notes,
        updated_at
"""

# Asset columns that create and update both write, in one place so the two
# statements cannot drift apart as the schema grows.
_ASSET_COLUMNS: Final = (
    "camera_type",
    "make",
    "model",
    "serial_number",
    "ip_address",
    "resolution",
    "codec",
    "frame_rate",
    "installed_on",
    "owner_org",
    "custodian_name",
    "custodian_contact",
    "nvr_reference",
    "nvr_channel",
    "retention_days",
    "recording_enabled",
    "site_name",
    "address",
    "ward",
    "zone",
    "district",
    "connectivity_type",
    "maintenance_state",
    "last_serviced_on",
    "next_service_due",
    "work_order_ref",
    "notes",
)

def _asset_assignments(
    payload: Any, params: list[Any], *, only_set_fields: bool
) -> list[tuple[str, str]]:
    """Return (column, placeholder) pairs for the asset columns to write.

    `only_set_fields` distinguishes the two calling conventions: an update must
    write only what the caller explicitly supplied, while a create may write
    every value the model resolved, defaults included.

    ip_address is cast because the column is INET and asyncpg sends a plain
    string, which Postgres will not coerce on its own.
    """
    pairs: list[tuple[str, str]] = []
    for column in _ASSET_COLUMNS:
        if only_set_fields and column not in payload.model_fields_set:
            continue
        value = getattr(payload, column, None)
        if only_set_fields is False and value is None:
            continue
        params.append(value)
        placeholder = f"${len(params)}"
        if column == "ip_address":
            placeholder += "::inet"
        pairs.append((column, placeholder))
    return pairs


# Sortable columns, mapped rather than interpolated: the value arrives from the
# query string and must never reach the statement as free text.
_SORTABLE_COLUMNS: Final = {
    "global_camera_code": "global_camera_code",
    "department_id": "department_id",
    "status": "status",
    "camera_type": "camera_type",
    "site_name": "site_name",
    "ward": "ward",
    "make": "make",
    "installed_on": "installed_on",
    "created_at": "created_at",
    "updated_at": "updated_at",
    "connectivity_status": "connectivity_status",
    "maintenance_state": "maintenance_state",
    "next_service_due": "next_service_due",
}

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


def bbox_params(
    min_lat: Annotated[float, Query(ge=-90.0, le=90.0)],
    min_lon: Annotated[float, Query(ge=-180.0, le=180.0)],
    max_lat: Annotated[float, Query(ge=-90.0, le=90.0)],
    max_lon: Annotated[float, Query(ge=-180.0, le=180.0)],
    limit: Annotated[int, Query(ge=1, le=10_000)] = 2_000,
    department_id: Annotated[str | None, Query(max_length=32)] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> BoundingBoxQuery:
    """Validate viewport parameters, giving a 422 rather than a 500 on nonsense."""
    try:
        return BoundingBoxQuery(
            min_lat=min_lat,
            min_lon=min_lon,
            max_lat=max_lat,
            max_lon=max_lon,
            limit=limit,
            department_id=department_id.upper() if department_id else None,
            status=status_filter.upper() if status_filter else None,
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
    principal: Annotated[Principal, Depends(require_role("DEPT_ADMIN"))],
    connection: Annotated[asyncpg.Connection, Depends(get_audited_connection)],
) -> CameraRead:
    """Ingest camera metadata and store its position as an EPSG:4326 point."""
    # A department admin may only onboard into their own department.
    principal.assert_can_access_department(payload.department_id)

    # Longitude before latitude: ST_MakePoint takes X first.
    params: list[Any] = [
        payload.global_camera_code,
        payload.department_id,
        payload.longitude,
        payload.latitude,
        payload.azimuth_angle,
        payload.fov_degrees,
        payload.stream_url,
        payload.vms_vendor,
        payload.status,
    ]
    columns = [
        "global_camera_code",
        "department_id",
        "azimuth_angle",
        "fov_degrees",
        "stream_url",
        "vms_vendor",
        "status",
    ]
    values = ["$1", "$2", "$5", "$6", "$7", "$8", "$9"]

    for column, placeholder in _asset_assignments(payload, params, only_set_fields=False):
        columns.append(column)
        values.append(placeholder)

    # location_geom is appended last so the $3/$4 pair above stays readable.
    columns.append("location_geom")
    values.append("ST_SetSRID(ST_MakePoint($3, $4), 4326)")

    insert_sql = f"""
        INSERT INTO cameras ({', '.join(columns)})
        VALUES ({', '.join(values)})
        RETURNING {_CAMERA_COLUMNS}
    """

    try:
        record = await connection.fetchrow(insert_sql, *params)
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

    camera = _project_camera(record, principal)
    logger.info(
        "camera registered",
        extra={
            "camera_id": str(camera.id),
            "global_camera_code": camera.global_camera_code,
            "department_id": camera.department_id,
            "actor": principal.label,
        },
    )
    return camera


@router.patch(
    "/{camera_id:uuid}",
    response_model=CameraRead,
    summary="Correct a registered camera's position or state",
)
async def update_camera(
    camera_id: UUID,
    payload: CameraUpdate,
    principal: Annotated[Principal, Depends(require_role("DEPT_ADMIN"))],
    connection: Annotated[asyncpg.Connection, Depends(get_audited_connection)],
) -> CameraRead:
    """Partial update of one camera row.

    This exists because a camera's position is frequently *not* known at
    registration time - the external live-feed grid, for instance, publishes
    only an id and a name, so every camera lands on a synthesized ring until
    someone surveys it. Re-registering is not an option: the row's UUID is
    already referenced by detections, alerts and relay sessions.

    Only the supplied fields are written. The SET clause is assembled from
    them rather than written out in full, so an omitted field is never
    overwritten with a default.
    """
    # Check ownership before building the statement: a department admin must not
    # be able to edit another department's camera, and finding out from a 404
    # after the write would be too late.
    owner = await connection.fetchval(
        "SELECT department_id FROM cameras WHERE id = $1", camera_id
    )
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"camera '{camera_id}' is not registered",
        )
    principal.assert_can_access_department(owner)

    assignments: list[str] = []
    params: list[Any] = []

    if payload.latitude is not None and payload.longitude is not None:
        # Same axis swap as create: ST_MakePoint takes X (longitude) first.
        params.extend([payload.longitude, payload.latitude])
        assignments.append(
            f"location_geom = ST_SetSRID(ST_MakePoint(${len(params) - 1}, ${len(params)}), 4326)"
        )

    for column, value in (
        ("azimuth_angle", payload.azimuth_angle),
        ("fov_degrees", payload.fov_degrees),
        ("stream_url", payload.stream_url),
        ("vms_vendor", payload.vms_vendor),
        ("status", payload.status),
    ):
        if value is not None:
            params.append(value)
            assignments.append(f"{column} = ${len(params)}")

    # Asset metadata: only columns the caller actually named, so an omitted
    # field is never overwritten and a deliberate null can still be written.
    for column, placeholder in _asset_assignments(payload, params, only_set_fields=True):
        assignments.append(f"{column} = {placeholder}")

    if not assignments:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="no updatable fields supplied",
        )

    params.append(camera_id)
    try:
        record = await connection.fetchrow(
            f"""
            UPDATE cameras
               SET {', '.join(assignments)}
             WHERE id = ${len(params)}
         RETURNING {_CAMERA_COLUMNS}
            """,
            *params,
        )
    except asyncpg.CheckViolationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"camera metadata rejected by a database constraint: {exc.constraint_name}",
        ) from exc

    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"camera '{camera_id}' is not registered",
        )

    camera = _project_camera(record, principal)
    logger.info(
        "camera updated",
        extra={
            "camera_id": str(camera.id),
            "global_camera_code": camera.global_camera_code,
            "fields": sorted(payload.model_fields_set),
            "actor": principal.label,
        },
    )
    return camera


@router.get(
    "",
    response_model=CameraListResponse,
    summary="List registered cameras",
)
async def list_cameras(
    principal: Annotated[Principal, Depends(get_current_principal)],
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: Annotated[int, Query(ge=1, le=100_000)] = 1_000,
    offset: Annotated[int, Query(ge=0)] = 0,
    q: Annotated[str | None, Query(max_length=120, description="Free-text search")] = None,
    department_id: Annotated[str | None, Query(max_length=32)] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    camera_type: Annotated[str | None, Query(max_length=20)] = None,
    connectivity: Annotated[str | None, Query(max_length=20)] = None,
    maintenance: Annotated[str | None, Query(max_length=20)] = None,
    ward: Annotated[str | None, Query(max_length=120)] = None,
    sort_by: Annotated[str, Query()] = "global_camera_code",
    order: Annotated[str, Query(pattern="^(asc|desc)$")] = "asc",
) -> CameraListResponse:
    """Fleet listing: the registry table and the GIS map both read from here."""
    effective_limit = min(limit, settings.max_list_results)

    params: list[Any] = []
    predicates: list[str] = _scope_predicate(principal, params, department_id)

    if status_filter is not None:
        params.append(status_filter.upper())
        predicates.append(f"status = ${len(params)}")

    for column, value in (
        ("camera_type", camera_type),
        ("connectivity_status", connectivity),
        ("maintenance_state", maintenance),
    ):
        if value:
            params.append(value.upper())
            predicates.append(f"{column} = ${len(params)}")

    if ward:
        params.append(ward)
        predicates.append(f"ward = ${len(params)}")

    if q:
        # One placeholder reused across the OR arms; the trigram indexes from
        # migration 003 are what keep this off a sequential scan.
        params.append(f"%{q}%")
        placeholder = f"${len(params)}"
        predicates.append(
            "("
            f"global_camera_code ILIKE {placeholder}"
            f" OR site_name ILIKE {placeholder}"
            f" OR make ILIKE {placeholder}"
            f" OR model ILIKE {placeholder}"
            f" OR ward ILIKE {placeholder}"
            f" OR owner_org ILIKE {placeholder}"
            ")"
        )

    where_clause = f"WHERE {' AND '.join(predicates)}" if predicates else ""

    # Whitelist, never interpolation: a sort column arriving from the query
    # string is user input and must not reach the statement unchecked.
    sort_column = _SORTABLE_COLUMNS.get(sort_by, "global_camera_code")
    direction = "DESC" if order == "desc" else "ASC"

    total = await connection.fetchval(
        f"SELECT count(*) FROM cameras {where_clause}", *params
    )

    params.extend([effective_limit, offset])
    records = await connection.fetch(
        f"""
        SELECT {_CAMERA_COLUMNS}
        FROM cameras
        {where_clause}
        ORDER BY {sort_column} {direction} NULLS LAST, global_camera_code ASC
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
        """,
        *params,
    )

    return CameraListResponse(
        items=[_project_camera(record, principal) for record in records],
        total=int(total or 0),
        limit=effective_limit,
        offset=offset,
    )


@router.get(
    "/bbox",
    response_model=CameraListResponse,
    summary="Cameras inside a map viewport",
)
async def cameras_in_bbox(
    query: Annotated[BoundingBoxQuery, Depends(bbox_params)],
    principal: Annotated[Principal, Depends(get_current_principal)],
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
) -> CameraListResponse:
    """Viewport load for the GIS map.

    The map previously fetched `?limit=10000` on mount, which is untenable at
    the 80,000-camera target. This fetches only what is on screen, served by the
    plain-geometry GIST index via ST_MakeEnvelope.
    """
    params: list[Any] = [query.min_lon, query.min_lat, query.max_lon, query.max_lat]
    predicates = [
        "location_geom && ST_MakeEnvelope($1, $2, $3, $4, 4326)"
    ]
    predicates.extend(_scope_predicate(principal, params, query.department_id))

    if query.status is not None:
        params.append(query.status)
        predicates.append(f"status = ${len(params)}")

    where_clause = " AND ".join(predicates)
    total = await connection.fetchval(
        f"SELECT count(*) FROM cameras WHERE {where_clause}", *params
    )

    params.append(query.limit)
    records = await connection.fetch(
        f"""
        SELECT {_CAMERA_COLUMNS}
        FROM cameras
        WHERE {where_clause}
        ORDER BY global_camera_code
        LIMIT ${len(params)}
        """,
        *params,
    )

    return CameraListResponse(
        items=[_project_camera(record, principal) for record in records],
        total=int(total or 0),
        limit=query.limit,
        offset=0,
    )


# ROUTE ORDER: the `:uuid` convertor is load-bearing, not decoration. FastAPI
# matches in declaration order, so a plain `/{camera_id}` declared before
# `/spatial-search` captures the literal as a path parameter and answers
# 422 "not a valid UUID". Constraining the segment to a UUID makes the literal
# routes match regardless of where they sit in this file.
@router.get(
    "/{camera_id:uuid}",
    response_model=CameraRead,
    summary="Read one camera",
)
async def get_camera(
    camera_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
) -> CameraRead:
    """Full metadata for one camera, for the registry detail screen."""
    record = await connection.fetchrow(
        f"SELECT {_CAMERA_COLUMNS} FROM cameras WHERE id = $1", camera_id
    )
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"camera '{camera_id}' is not registered",
        )
    principal.assert_can_access_department(record["department_id"])
    return _project_camera(record, principal)


@router.delete(
    "/{camera_id:uuid}",
    response_model=CameraRead,
    summary="Decommission a camera",
)
async def decommission_camera(
    camera_id: UUID,
    principal: Annotated[Principal, Depends(require_role("DEPT_ADMIN"))],
    connection: Annotated[asyncpg.Connection, Depends(get_audited_connection)],
) -> CameraRead:
    """Soft delete: the row is marked DECOMMISSIONED, never removed.

    Detections, alerts and relay sessions reference this id. Deleting the row
    would either cascade evidence away or orphan it; neither is acceptable for
    a system of record, and a decommissioned camera is itself a fact worth
    keeping for audit.
    """
    owner = await connection.fetchval(
        "SELECT department_id FROM cameras WHERE id = $1", camera_id
    )
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"camera '{camera_id}' is not registered",
        )
    principal.assert_can_access_department(owner)

    record = await connection.fetchrow(
        f"""
        UPDATE cameras
           SET status = 'DECOMMISSIONED',
               connectivity_status = 'UNKNOWN',
               recording_enabled = FALSE
         WHERE id = $1
     RETURNING {_CAMERA_COLUMNS}
        """,
        camera_id,
    )

    logger.info(
        "camera decommissioned",
        extra={"camera_id": str(camera_id), "actor": principal.label},
    )
    return _project_camera(record, principal)


@router.get(
    "/{camera_id:uuid}/health",
    response_model=CameraHealthRead,
    summary="Latest reachability state of one camera",
)
async def camera_health(
    camera_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
) -> CameraHealthRead:
    """Most recent `camera_health_logs` row for a camera.

    Served from the `(camera_id, logged_at DESC)` index, so it stays a single
    index lookup no matter how deep the telemetry history grows.
    """
    owner = await connection.fetchval(
        "SELECT department_id FROM cameras WHERE id = $1", camera_id
    )
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"camera '{camera_id}' is not registered",
        )
    principal.assert_can_access_department(owner)

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
    principal: Annotated[Principal, Depends(get_current_principal)],
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

    predicates.extend(_scope_predicate(principal, params, query.department_id))

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
    return [_project_camera(record, principal) for record in records]


@router.post(
    "/health-ping",
    response_model=HealthPingResult,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Bulk-record camera reachability checks",
)
async def health_ping(
    payload: HealthPingBatch,
    principal: Annotated[Principal, Depends(require_role("OPERATOR"))],
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HealthPingResult:
    """Insert a batch of health checks into ``camera_health_logs`` atomically.

    The fleet poller authenticates with a service API key rather than a human
    login - see the api_keys table.
    """
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
