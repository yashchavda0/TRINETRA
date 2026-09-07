-- ============================================================================
-- Gujarat Police CCTV Integration System
-- Migration 001 : Model 1 - Central GIS Camera Registry
--
-- Target platform : PostgreSQL 16 + PostGIS 3.4
-- Spatial reference: WGS84 / EPSG:4326 exclusively
-- Timestamps       : TIMESTAMPTZ, stored UTC (set the session/cluster to UTC)
--
-- Apply with:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/001_init_model1_registry.sql
--
-- The whole migration runs in one transaction: it either lands completely or
-- rolls back completely. Every statement is idempotent, so re-running is safe.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- Extensions
-- ---------------------------------------------------------------------------
-- PostGIS supplies GEOMETRY/GEOGRAPHY types, the GIST spatial index operator
-- classes, and ST_* functions.
CREATE EXTENSION IF NOT EXISTS postgis;

-- gen_random_uuid() is part of core PostgreSQL from version 13 onward, so no
-- pgcrypto extension is required on PostgreSQL 16.

-- ---------------------------------------------------------------------------
-- Table: departments
-- ---------------------------------------------------------------------------
-- Owning agencies of the cameras in the fleet. The primary key is the short
-- department code, kept byte-identical to the DepartmentCode enum member names
-- in proto/surveillance_event.proto so that an event's department_code maps
-- straight onto a registry row with no translation table.
CREATE TABLE IF NOT EXISTS departments (
    id            VARCHAR(32) PRIMARY KEY,
    name          TEXT NOT NULL,
    contact_email TEXT
);

COMMENT ON TABLE  departments IS
    'Owning agencies of registered cameras. id matches the DepartmentCode enum in proto/surveillance_event.proto.';
COMMENT ON COLUMN departments.id IS
    'Short department code, e.g. POLICE. Mirrors the protobuf DepartmentCode enum member name.';
COMMENT ON COLUMN departments.contact_email IS
    'Operational escalation mailbox for camera outages in this department.';

-- Seed the six known departments so the cameras.department_id foreign key is
-- immediately satisfiable. ON CONFLICT DO NOTHING keeps re-runs harmless and
-- never overwrites a contact_email an operator has since corrected.
INSERT INTO departments (id, name, contact_email) VALUES
    ('POLICE',          'Gujarat Police',                        NULL),
    ('RTO',             'Regional Transport Office',             NULL),
    ('GSRTC',           'Gujarat State Road Transport Corporation', NULL),
    ('CIVIL_SUPPLIES',  'Department of Food and Civil Supplies',  NULL),
    ('REVENUE',         'Revenue Department',                     NULL),
    ('PRIVATE',         'Private Operator',                       NULL)
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Table: cameras
-- ---------------------------------------------------------------------------
-- The authoritative record of every camera in the integrated fleet
-- (target scale 80,000+ rows). cameras.id is the UUID referenced by
-- SurveillanceEvent.camera_id on the analytics bus.
CREATE TABLE IF NOT EXISTS cameras (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    global_camera_code VARCHAR(64) UNIQUE NOT NULL,
    department_id      VARCHAR(32) REFERENCES departments(id) ON DELETE RESTRICT,
    location_geom      GEOMETRY(Point, 4326) NOT NULL,
    azimuth_angle      NUMERIC(5, 2) CHECK (azimuth_angle >= 0 AND azimuth_angle <= 360),
    fov_degrees        NUMERIC(5, 2) DEFAULT 70.0
                           CHECK (fov_degrees > 0 AND fov_degrees <= 360),
    stream_url         TEXT NOT NULL,
    vms_vendor         VARCHAR(64),
    status             VARCHAR(20) DEFAULT 'ACTIVE'
                           CHECK (status IN ('ACTIVE', 'INACTIVE', 'MAINTENANCE', 'DECOMMISSIONED')),
    created_at         TIMESTAMPTZ DEFAULT clock_timestamp()
);

COMMENT ON TABLE  cameras IS
    'Model 1 Central GIS Camera Registry: single spatial source of truth for the integrated CCTV fleet.';
COMMENT ON COLUMN cameras.id IS
    'Registry UUID. Published on the analytics bus as SurveillanceEvent.camera_id.';
COMMENT ON COLUMN cameras.global_camera_code IS
    'Human-readable fleet-wide unique code, e.g. AHM-TRF-00147. Natural key used by field teams.';
COMMENT ON COLUMN cameras.location_geom IS
    'Mount position as a WGS84 (EPSG:4326) point. Longitude is X, latitude is Y: build with ST_SetSRID(ST_MakePoint(longitude, latitude), 4326).';
COMMENT ON COLUMN cameras.azimuth_angle IS
    'Compass bearing the camera faces in degrees: 0 = true north, increasing clockwise, range 0-360.';
COMMENT ON COLUMN cameras.fov_degrees IS
    'Horizontal field of view in degrees. Combined with azimuth_angle to compute the covered sector.';
COMMENT ON COLUMN cameras.stream_url IS
    'Live stream endpoint (RTSP/RTSPS) or ONVIF device service URL. Treat as a credential-bearing secret.';
COMMENT ON COLUMN cameras.vms_vendor IS
    'Video Management System vendor operating this camera, e.g. Milestone, Genetec, Honeywell.';
COMMENT ON COLUMN cameras.created_at IS
    'Registration instant, UTC. clock_timestamp() so rows inserted in one bulk transaction retain distinct times.';

-- Indexes on cameras -------------------------------------------------------
-- GIST spatial index on the geometry: serves degree-space predicates such as
-- ST_Intersects and ST_MakeEnvelope viewport queries, instead of a sequential
-- scan of 80,000+ rows.
CREATE INDEX IF NOT EXISTS idx_cameras_location_geom
    ON cameras USING GIST (location_geom);

-- Second GIST index on the GEOGRAPHY cast of the same column. This one is
-- mandatory, not a duplicate: metre-accurate radius search uses
-- ST_DWithin(location_geom::geography, ..., radius_metres), and the planner
-- cannot use the plain geometry index for a casted expression. Without this
-- index every radius search degrades to a full scan with a per-row cast.
CREATE INDEX IF NOT EXISTS idx_cameras_location_geog
    ON cameras USING GIST ((location_geom::geography));

-- Per-department fleet listings and department-filtered spatial searches.
CREATE INDEX IF NOT EXISTS idx_cameras_department_id
    ON cameras (department_id);

-- Health dashboards filter hard on status.
CREATE INDEX IF NOT EXISTS idx_cameras_status
    ON cameras (status);

-- ---------------------------------------------------------------------------
-- Table: camera_health_logs
-- ---------------------------------------------------------------------------
-- Append-only reachability telemetry. This is the hottest write path in the
-- system: one row per camera per polling cycle across 80,000+ cameras.
CREATE TABLE IF NOT EXISTS camera_health_logs (
    id              BIGSERIAL PRIMARY KEY,
    camera_id       UUID REFERENCES cameras(id) ON DELETE CASCADE,
    ping_latency_ms INT CHECK (ping_latency_ms IS NULL OR ping_latency_ms >= 0),
    is_reachable    BOOLEAN NOT NULL,
    logged_at       TIMESTAMPTZ DEFAULT clock_timestamp()
);

COMMENT ON TABLE  camera_health_logs IS
    'Append-only camera reachability telemetry, one row per camera per health polling cycle.';
COMMENT ON COLUMN camera_health_logs.ping_latency_ms IS
    'Round-trip latency in milliseconds. NULL when the camera was unreachable and no RTT exists.';
COMMENT ON COLUMN camera_health_logs.logged_at IS
    'Instant the health check completed, UTC.';

-- Composite index oriented at the dominant read pattern: "latest health rows
-- for this camera". DESC on logged_at lets the planner satisfy the ORDER BY
-- from the index without a sort.
CREATE INDEX IF NOT EXISTS idx_camera_health_logs_camera_logged_at
    ON camera_health_logs (camera_id, logged_at DESC);

-- Fleet-wide outage sweeps: "everything unreachable in the last N minutes".
CREATE INDEX IF NOT EXISTS idx_camera_health_logs_unreachable
    ON camera_health_logs (logged_at DESC)
    WHERE is_reachable = FALSE;

COMMIT;
