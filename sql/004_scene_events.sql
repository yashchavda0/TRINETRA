-- ============================================================================
-- Gujarat Police CCTV Integration System (TRINETRA)
-- Migration 004 : scene events — agentic video understanding (Tier B)
--
-- Target platform : PostgreSQL 16 + PostGIS 3.4
-- Spatial reference: WGS84 / EPSG:4326 exclusively
-- Timestamps       : epoch milliseconds (BIGINT) for bus-sourced instants,
--                    TIMESTAMPTZ (UTC) for server-side bookkeeping
--
-- Depends on 001_init_model1_registry.sql, 002_pipeline_state.sql,
-- 003_asset_metadata_and_identity.sql.
--
-- Apply with:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/004_scene_events.sql
--
-- WHY A SEPARATE TABLE FROM detections
--
-- services/vlm_agent's Tier B (agentic anomaly/event reasoning: loitering,
-- wrong-way, collision, crowd density) produces a fundamentally different
-- kind of row than a SurveillanceEvent detection: temporal (spans a window,
-- not an instant), possibly multi-object, carries a free-text rationale and
-- an operator workflow state. That is the same shape of reasoning that made
-- threat_alerts a separate table from detections in 002, not a new column on
-- it - same precedent, same reasoning, applied here.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS scene_events (
    scene_event_id        UUID PRIMARY KEY,
    camera_id             UUID NOT NULL,
    window_start_utc_ms   BIGINT NOT NULL CHECK (window_start_utc_ms > 0),
    window_end_utc_ms     BIGINT NOT NULL CHECK (window_end_utc_ms >= window_start_utc_ms),
    event_type            VARCHAR(32) NOT NULL,
    confidence            DOUBLE PRECISION NOT NULL DEFAULT 0.0
                              CHECK (confidence >= 0 AND confidence <= 1),
    rationale             TEXT,
    implicated_target_ids TEXT[] NOT NULL DEFAULT '{}',
    clip_uri              TEXT,
    model_version         VARCHAR(64),
    scene_event_geom      GEOMETRY(Point, 4326),
    workflow_state        VARCHAR(20) NOT NULL DEFAULT 'NEW'
                              CHECK (workflow_state IN ('NEW', 'ACKNOWLEDGED', 'DISMISSED')),
    ingested_at           TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

COMMENT ON TABLE scene_events IS
    'One row per SceneEvent off scene-events-raw: an agentic (VLM) anomaly/event finding over a camera''s trigger window. Not a per-object detection - see detections and threat_alerts for those.';
COMMENT ON COLUMN scene_events.scene_event_id IS
    'UUIDv4 idempotency key from the SceneEvent proto, matching the at-least-once delivery convention SurveillanceEvent.event_id already establishes.';
COMMENT ON COLUMN scene_events.implicated_target_ids IS
    'Re-ID target_ids (engine/vector_matcher.py identities) this finding implicates, when attributable. May be empty, e.g. for a crowd-density finding.';
COMMENT ON COLUMN scene_events.workflow_state IS
    'Operator triage state, independent of the alert path: a high-severity finding also reaches threat_alerts, but every finding lands here regardless of severity.';

-- Deliberately NO foreign key on camera_id, same rationale as detections in
-- 002: a registry gap must not drop an operationally important finding.
CREATE INDEX IF NOT EXISTS idx_scene_events_camera_time
    ON scene_events (camera_id, window_start_utc_ms DESC);

CREATE INDEX IF NOT EXISTS idx_scene_events_type_time
    ON scene_events (event_type, window_start_utc_ms DESC);

CREATE INDEX IF NOT EXISTS idx_scene_events_workflow
    ON scene_events (workflow_state, window_start_utc_ms DESC)
    WHERE workflow_state <> 'DISMISSED';

CREATE INDEX IF NOT EXISTS idx_scene_events_geom
    ON scene_events USING GIST (scene_event_geom);

COMMIT;
