"""Registry analytics: coverage, density, ageing infrastructure and health.

These are the Model 1 "gap analysis" deliverables. Every one of them is
computed in PostGIS from columns the registry already holds - notably
``azimuth_angle`` and ``fov_degrees``, which were stored and constrained from
the first migration and, until now, read by nothing.

A note on what a coverage polygon means here: it is the *nominal* sector a
camera faces, out to a configurable range. It is not a line-of-sight analysis -
it does not know about buildings, trees or a tilted mount. Treated as "where we
have no camera pointing at all", it is sound; treated as "what is actually
visible", it is optimistic. The API says so in its own response.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Final

import asyncpg
from fastapi import APIRouter, Depends, Query

from app.auth.dependencies import Principal, get_current_principal
from app.database import get_connection
from app.schemas import (
    AgeingBand,
    AgeingReport,
    CoverageReport,
    CoverageSummary,
    DensityRow,
    FleetHealthSummary,
    MaintenanceDueRow,
)

logger: Final = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])

# The viewshed wedge. ST_Project walks a true geodesic from the camera at a
# compass bearing, so the arc is metre-accurate at any latitude; the polygon is
# closed back through the camera position to make a sector rather than an arc.
#
# Cameras with no surveyed azimuth get a full circle instead: an unknown
# direction must not silently become "facing north", which a COALESCE to zero
# would do.
_WEDGE_CTE: Final = """
    WITH params AS (
        SELECT $1::double precision AS range_m, 24 AS steps
    ),
    footprints AS (
        SELECT
            c.id,
            c.department_id,
            c.global_camera_code,
            CASE
                WHEN c.azimuth_angle IS NULL OR c.fov_degrees IS NULL OR c.fov_degrees >= 359
                THEN ST_Buffer(c.location_geom::geography, p.range_m)::geometry
                ELSE ST_MakePolygon(
                    ST_MakeLine(
                        ARRAY[c.location_geom]
                        || ARRAY(
                            SELECT ST_Project(
                                c.location_geom::geography,
                                p.range_m,
                                radians(
                                    c.azimuth_angle::double precision
                                    - c.fov_degrees::double precision / 2.0
                                    + (c.fov_degrees::double precision * s / p.steps)
                                )
                            )::geometry
                            FROM generate_series(0, p.steps) AS s
                        )
                        || ARRAY[c.location_geom]
                    )
                )
            END AS geom
        FROM cameras c, params p
        WHERE c.status = 'ACTIVE'
    )
"""


def _scope_clause(principal: Principal, params: list[Any], alias: str = "c") -> str:
    """Department restriction applied to every report, from the token."""
    scope = principal.department_scope
    if scope is None:
        return ""
    params.append(scope)
    return f" AND {alias}.department_id = ${len(params)}"


@router.get(
    "/coverage",
    response_model=CoverageReport,
    summary="Camera coverage footprint and uncovered area",
)
async def coverage_report(
    principal: Annotated[Principal, Depends(get_current_principal)],
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    range_meters: Annotated[
        float,
        Query(gt=0, le=2000, description="Nominal effective range of each camera"),
    ] = 120.0,
    boundary_level: Annotated[str | None, Query(max_length=20)] = None,
    include_geometry: Annotated[
        bool, Query(description="Return the merged footprint as GeoJSON for map overlay")
    ] = True,
) -> CoverageReport:
    """Union every camera's viewshed and report covered versus uncovered area."""
    # $1 range, $2 geometry flag, then any scope parameter. The order is fixed
    # here, before _scope_clause appends, so a department-scoped caller cannot
    # end up with the boolean and the scope swapped.
    params: list[Any] = [range_meters, include_geometry]
    scope_sql = _scope_clause(principal, params, alias="c")

    # ST_Union of the whole fleet, then area on the geography cast so the number
    # is true square metres rather than square degrees.
    footprint_sql = f"""
        {_WEDGE_CTE}
        SELECT
            count(*)                                            AS camera_count,
            ST_Area(ST_Union(f.geom)::geography)                AS covered_sq_m,
            CASE WHEN $2::boolean
                 THEN ST_AsGeoJSON(ST_Union(f.geom), 6)
                 ELSE NULL
            END                                                 AS footprint
        FROM footprints f
        JOIN cameras c ON c.id = f.id
        WHERE TRUE {scope_sql}
    """
    record = await connection.fetchrow(footprint_sql, *params)

    covered = float(record["covered_sq_m"] or 0.0)
    camera_count = int(record["camera_count"] or 0)

    # Per-boundary breakdown, which is what makes a gap actionable: "37% of
    # Navrangpura is covered" is a work order, "4.1 km2 covered" is trivia.
    boundary_params: list[Any] = [range_meters]
    boundary_scope = _scope_clause(principal, boundary_params, alias="c")
    level_filter = ""
    if boundary_level:
        boundary_params.append(boundary_level.upper())
        level_filter = f"WHERE b.level = ${len(boundary_params)}"

    boundary_rows = await connection.fetch(
        f"""
        {_WEDGE_CTE},
        scoped AS (
            SELECT f.* FROM footprints f
            JOIN cameras c ON c.id = f.id
            WHERE TRUE {boundary_scope}
        )
        SELECT
            b.id::text                                   AS boundary_id,
            b.name,
            b.level,
            ST_Area(b.geom::geography)                   AS area_sq_m,
            COALESCE(
                ST_Area(
                    ST_Intersection(
                        ST_Union(s.geom),
                        b.geom
                    )::geography
                ), 0
            )                                            AS covered_sq_m,
            count(s.id)                                  AS camera_count
        FROM admin_boundaries b
        LEFT JOIN scoped s ON ST_Intersects(s.geom, b.geom)
        {level_filter}
        GROUP BY b.id, b.name, b.level, b.geom
        -- The ratio is spelled out rather than reusing the covered_sq_m alias:
        -- Postgres accepts an output alias as a bare ORDER BY term but not
        -- inside an expression, where it reads as an undefined column.
        ORDER BY
            COALESCE(
                ST_Area(ST_Intersection(ST_Union(s.geom), b.geom)::geography), 0
            ) / NULLIF(ST_Area(b.geom::geography), 0) ASC NULLS FIRST
        """,
        *boundary_params,
    )

    summaries = [
        CoverageSummary(
            boundary_id=row["boundary_id"],
            name=row["name"],
            level=row["level"],
            area_sq_km=round(float(row["area_sq_m"] or 0) / 1_000_000, 4),
            covered_sq_km=round(float(row["covered_sq_m"] or 0) / 1_000_000, 4),
            coverage_percent=(
                round(100.0 * float(row["covered_sq_m"] or 0) / float(row["area_sq_m"]), 2)
                if row["area_sq_m"]
                else 0.0
            ),
            camera_count=int(row["camera_count"] or 0),
        )
        for row in boundary_rows
    ]

    return CoverageReport(
        range_meters=range_meters,
        camera_count=camera_count,
        covered_sq_km=round(covered / 1_000_000, 4),
        boundaries=summaries,
        footprint_geojson=record["footprint"],
        caveat=(
            "Nominal sectors from azimuth and field of view at the given range. "
            "No line-of-sight modelling: obstructions, mounting height and tilt "
            "are not considered, so treat this as an upper bound on coverage."
        ),
    )


@router.get(
    "/density",
    response_model=list[DensityRow],
    summary="Camera density per administrative area",
)
async def density_report(
    principal: Annotated[Principal, Depends(get_current_principal)],
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    level: Annotated[str, Query(max_length=20)] = "WARD",
) -> list[DensityRow]:
    """Cameras per square kilometre, by boundary."""
    params: list[Any] = [level.upper()]
    scope_sql = _scope_clause(principal, params, alias="c")

    rows = await connection.fetch(
        f"""
        SELECT
            b.id::text                        AS boundary_id,
            b.name,
            b.level,
            ST_Area(b.geom::geography) / 1000000.0 AS area_sq_km,
            b.population,
            count(c.id)                       AS camera_count
        FROM admin_boundaries b
        LEFT JOIN cameras c
               ON c.status = 'ACTIVE'
              AND ST_Contains(b.geom, c.location_geom)
              {scope_sql}
        WHERE b.level = $1
        GROUP BY b.id, b.name, b.level, b.geom, b.population
        ORDER BY camera_count DESC
        """,
        *params,
    )

    return [
        DensityRow(
            boundary_id=row["boundary_id"],
            name=row["name"],
            level=row["level"],
            area_sq_km=round(float(row["area_sq_km"] or 0), 4),
            camera_count=int(row["camera_count"] or 0),
            cameras_per_sq_km=(
                round(int(row["camera_count"] or 0) / float(row["area_sq_km"]), 3)
                if row["area_sq_km"]
                else 0.0
            ),
            population=row["population"],
        )
        for row in rows
    ]


@router.get(
    "/ageing",
    response_model=AgeingReport,
    summary="Ageing infrastructure by installation date",
)
async def ageing_report(
    principal: Annotated[Principal, Depends(get_current_principal)],
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    replacement_years: Annotated[int, Query(ge=1, le=30)] = 7,
) -> AgeingReport:
    """Age bands from ``installed_on``.

    Deliberately not from ``created_at``: that is when the row was written, so a
    2016 camera imported last week would report as new and the whole report
    would be wrong by the working life of the asset.
    """
    params: list[Any] = []
    scope_sql = _scope_clause(principal, params, alias="c")

    rows = await connection.fetch(
        f"""
        SELECT
            CASE
                WHEN c.installed_on IS NULL THEN 'UNKNOWN'
                WHEN c.installed_on > CURRENT_DATE - INTERVAL '2 years'  THEN '0-2 years'
                WHEN c.installed_on > CURRENT_DATE - INTERVAL '5 years'  THEN '2-5 years'
                WHEN c.installed_on > CURRENT_DATE - INTERVAL '8 years'  THEN '5-8 years'
                ELSE '8+ years'
            END                                   AS band,
            count(*)                              AS camera_count
        FROM cameras c
        WHERE c.status <> 'DECOMMISSIONED' {scope_sql}
        GROUP BY band
        ORDER BY band
        """,
        *params,
    )

    due_params: list[Any] = [replacement_years]
    due_scope = _scope_clause(principal, due_params, alias="c")
    due = await connection.fetchval(
        f"""
        SELECT count(*) FROM cameras c
        WHERE c.status <> 'DECOMMISSIONED'
          AND c.installed_on IS NOT NULL
          -- make_interval, not ($1 || ' years')::interval: asyncpg sends a
          -- typed int4 and Postgres has no int || text operator, so the string
          -- form works in psql (where the literal is untyped) and fails here.
          AND c.installed_on <= CURRENT_DATE - make_interval(years => $1::int)
          {due_scope}
        """,
        *due_params,
    )

    unknown_params: list[Any] = []
    unknown_scope = _scope_clause(principal, unknown_params, alias="c")
    unknown = await connection.fetchval(
        f"""
        SELECT count(*) FROM cameras c
        WHERE c.status <> 'DECOMMISSIONED' AND c.installed_on IS NULL {unknown_scope}
        """,
        *unknown_params,
    )

    return AgeingReport(
        replacement_years=replacement_years,
        due_for_replacement=int(due or 0),
        unknown_installation_date=int(unknown or 0),
        bands=[
            AgeingBand(band=row["band"], camera_count=int(row["camera_count"]))
            for row in rows
        ],
    )


@router.get(
    "/health-summary",
    response_model=FleetHealthSummary,
    summary="Fleet-wide health, uptime and maintenance rollup",
)
async def health_summary(
    principal: Annotated[Principal, Depends(get_current_principal)],
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    window_hours: Annotated[int, Query(ge=1, le=720)] = 24,
) -> FleetHealthSummary:
    """One request for what previously needed one call per camera.

    The per-camera endpoint answers "is this camera up"; a fleet dashboard
    asking that 80,000 times is not a dashboard. This aggregates in the
    database instead.
    """
    params: list[Any] = [window_hours]
    scope_sql = _scope_clause(principal, params, alias="c")

    # The lateral join takes the newest health row per camera off the
    # (camera_id, logged_at DESC) index rather than scanning the log.
    record = await connection.fetchrow(
        f"""
        WITH scoped AS (
            SELECT c.id, c.status, c.maintenance_state
            FROM cameras c
            WHERE c.status <> 'DECOMMISSIONED' {scope_sql}
        ),
        latest AS (
            SELECT s.id, h.is_reachable, h.logged_at
            FROM scoped s
            LEFT JOIN LATERAL (
                SELECT is_reachable, logged_at
                FROM camera_health_logs
                WHERE camera_id = s.id
                ORDER BY logged_at DESC
                LIMIT 1
            ) h ON TRUE
        ),
        window_stats AS (
            SELECT
                count(*) FILTER (WHERE is_reachable)                AS reachable_pings,
                count(*)                                            AS total_pings
            FROM camera_health_logs
            WHERE logged_at >= clock_timestamp() - make_interval(hours => $1::int)
              AND camera_id IN (SELECT id FROM scoped)
        )
        SELECT
            (SELECT count(*) FROM scoped)                                   AS total,
            count(*) FILTER (WHERE l.is_reachable IS TRUE)                  AS up,
            count(*) FILTER (WHERE l.is_reachable IS FALSE)                 AS down,
            count(*) FILTER (WHERE l.is_reachable IS NULL)                  AS unknown,
            (SELECT count(*) FROM scoped WHERE maintenance_state IN ('DUE','OVERDUE','FAULTY'))
                                                                            AS maintenance_attention,
            (SELECT reachable_pings FROM window_stats)                      AS reachable_pings,
            (SELECT total_pings FROM window_stats)                          AS total_pings
        FROM latest l
        """,
        *params,
    )

    total_pings = int(record["total_pings"] or 0)
    uptime = (
        round(100.0 * int(record["reachable_pings"] or 0) / total_pings, 2)
        if total_pings
        else None
    )

    due_params: list[Any] = []
    due_scope = _scope_clause(principal, due_params, alias="c")
    due_rows = await connection.fetch(
        f"""
        SELECT c.id::text, c.global_camera_code, c.site_name, c.maintenance_state,
               c.next_service_due, c.work_order_ref
        FROM cameras c
        WHERE c.status <> 'DECOMMISSIONED'
          AND (
                c.maintenance_state IN ('DUE', 'OVERDUE', 'FAULTY')
             OR (c.next_service_due IS NOT NULL AND c.next_service_due <= CURRENT_DATE)
          )
          {due_scope}
        ORDER BY c.next_service_due NULLS LAST
        LIMIT 200
        """,
        *due_params,
    )

    return FleetHealthSummary(
        window_hours=window_hours,
        total_cameras=int(record["total"] or 0),
        up=int(record["up"] or 0),
        down=int(record["down"] or 0),
        unknown=int(record["unknown"] or 0),
        uptime_percent=uptime,
        pings_in_window=total_pings,
        maintenance_attention=int(record["maintenance_attention"] or 0),
        maintenance_due=[
            MaintenanceDueRow(
                camera_id=row["id"],
                global_camera_code=row["global_camera_code"],
                site_name=row["site_name"],
                maintenance_state=row["maintenance_state"],
                next_service_due=row["next_service_due"],
                work_order_ref=row["work_order_ref"],
            )
            for row in due_rows
        ],
    )
