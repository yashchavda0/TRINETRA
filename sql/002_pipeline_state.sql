-- ============================================================================
-- Gujarat Police CCTV Integration System (TRINETRA)
-- Migration 002 : pipeline state — detections, Re-ID, wake-up, alerts, relay
--
-- Target platform : PostgreSQL 16 + PostGIS 3.4
-- Spatial reference: WGS84 / EPSG:4326 exclusively
-- Timestamps       : epoch milliseconds (BIGINT) for bus-sourced instants,
--                    TIMESTAMPTZ (UTC) for server-side bookkeeping
--
-- Depends on 001_init_model1_registry.sql.
--
-- Apply with:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/002_pipeline_state.sql
--
-- Until this migration is applied, every stage past the registry is in-process
-- memory only: Re-ID vectors, camera wake-up state, dispatched alerts and relay
-- sessions all die with the worker that produced them.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- Table: camera_transition_stats
-- ---------------------------------------------------------------------------
-- Observed median transit time per ordered camera pair. engine/tracking_pipeline.py
-- already queries this table (_TRANSIT_TIME_QUERY) and silently degrades to
-- d_ij / 11.1 m/s when it is absent, so creating it is what lets the STGCN prior
-- see measured travel times instead of a fleet-wide guess.
CREATE TABLE IF NOT EXISTS camera_transition_stats (
    source_camera_id       UUID NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    target_camera_id       UUID NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    median_transit_seconds DOUBLE PRECISION NOT NULL
                               CHECK (median_transit_seconds > 0),
    sample_count           INTEGER NOT NULL DEFAULT 0 CHECK (sample_count >= 0),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (source_camera_id, target_camera_id),
    CHECK (source_camera_id <> target_camera_id)
);

COMMENT ON TABLE camera_transition_stats IS
    'Measured median transit time per ordered camera pair; feeds tau_ij in the Model 4 handoff prior.';
COMMENT ON COLUMN camera_transition_stats.median_transit_seconds IS
    'Median seconds for a target to travel source -> target, recomputed by workers/seed_transition_stats.py.';
COMMENT ON COLUMN camera_transition_stats.sample_count IS
    'Number of observed handoffs behind the median. Treat a low count as an unreliable estimate.';

-- ---------------------------------------------------------------------------
-- Table: detections
-- ---------------------------------------------------------------------------
-- One row per SurveillanceEvent consumed off the analytics bus. event_id is the
-- idempotency key the protobuf contract designates for at-least-once delivery,
-- so the worker inserts with ON CONFLICT DO NOTHING.
--
-- Deliberately NO foreign key on camera_id: the bus must not lose evidence
-- because a camera is missing from the registry. Registry gaps are a data-quality
-- report, not a reason to drop a detection.
CREATE TABLE IF NOT EXISTS detections (
    event_id            UUID PRIMARY KEY,
    camera_id           UUID NOT NULL,
    department_id       VARCHAR(32),
    department_code_tag SMALLINT,
    timestamp_utc_ms    BIGINT NOT NULL CHECK (timestamp_utc_ms > 0),
    object_class        VARCHAR(32) NOT NULL DEFAULT 'OBJECT_CLASS_UNSPECIFIED',
    bbox_x_min          REAL,
    bbox_y_min          REAL,
    bbox_x_max          REAL,
    bbox_y_max          REAL,
    attributes          JSONB NOT NULL DEFAULT '{}'::jsonb,
    track_id            TEXT,
    target_id           TEXT,
    detection_geom      GEOMETRY(Point, 4326),
    azimuth_degrees     NUMERIC(5, 2),
    embedding_accepted  BOOLEAN NOT NULL DEFAULT TRUE,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

COMMENT ON TABLE detections IS
    'One row per SurveillanceEvent off the bus. event_id is the at-least-once idempotency key.';
COMMENT ON COLUMN detections.department_code_tag IS
    'Numeric protobuf DepartmentCode tag as published on the Kafka key. department_id holds the resolved textual registry code.';
COMMENT ON COLUMN detections.timestamp_utc_ms IS
    'Frame capture instant from the producer, UTC Unix epoch milliseconds.';
COMMENT ON COLUMN detections.detection_geom IS
    'Observing camera position snapshotted from the event, WGS84 (EPSG:4326).';
COMMENT ON COLUMN detections.target_id IS
    'Re-ID identity this detection was assigned to, or NULL when no embedding was usable.';
COMMENT ON COLUMN detections.embedding_accepted IS
    'FALSE when feature_embedding was not exactly 512 floats; the row is kept as evidence, the vector is not.';

CREATE INDEX IF NOT EXISTS idx_detections_camera_time
    ON detections (camera_id, timestamp_utc_ms DESC);

CREATE INDEX IF NOT EXISTS idx_detections_track_id
    ON detections (track_id)
    WHERE track_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_detections_target_time
    ON detections (target_id, timestamp_utc_ms DESC)
    WHERE target_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_detections_geom
    ON detections USING GIST (detection_geom);

-- ---------------------------------------------------------------------------
-- Table: target_embeddings
-- ---------------------------------------------------------------------------
-- Persisted Re-ID vectors so ActiveTargetsCache can be rehydrated after a worker
-- restart instead of losing every tracked identity.
--
-- REAL[] with a length CHECK rather than pgvector: the postgis/postgis:16-3.4
-- image ships no vector extension, and similarity scoring happens in torch on
-- the worker, not in SQL. Revisit when fleet-scale ANN search is needed in the
-- database itself — at 80,000 cameras a sequential array scan will not do.
CREATE TABLE IF NOT EXISTS target_embeddings (
    target_id           TEXT PRIMARY KEY,
    embedding           REAL[] NOT NULL
                            CHECK (array_length(embedding, 1) = 512),
    last_seen_camera_id UUID,
    last_seen_utc_ms    BIGINT,
    sighting_count      INTEGER NOT NULL DEFAULT 1 CHECK (sighting_count > 0),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

COMMENT ON TABLE target_embeddings IS
    'Persisted 512-dimension Re-ID feature vectors, one row per tracked identity.';
COMMENT ON COLUMN target_embeddings.embedding IS
    'Exactly 512 float32 values, L2-normalised. The CHECK enforces the dimension the bus contract mandates.';

CREATE INDEX IF NOT EXISTS idx_target_embeddings_last_seen
    ON target_embeddings (last_seen_utc_ms DESC);

-- ---------------------------------------------------------------------------
-- Table: camera_wake_state
-- ---------------------------------------------------------------------------
-- Cross-process mirror of engine.vector_matcher.CameraStateRecord. Without it the
-- PASSIVE -> PRE_ACTIVATED -> ACTIVE_TRACKING -> COOLDOWN machine is invisible to
-- operators and to any second worker replica.
CREATE TABLE IF NOT EXISTS camera_wake_state (
    camera_id           UUID PRIMARY KEY REFERENCES cameras(id) ON DELETE CASCADE,
    state               VARCHAR(20) NOT NULL DEFAULT 'PASSIVE'
                            CHECK (state IN ('PASSIVE', 'PRE_ACTIVATED', 'ACTIVE_TRACKING', 'COOLDOWN')),
    target_id           TEXT,
    handoff_probability DOUBLE PRECISION NOT NULL DEFAULT 0.0
                            CHECK (handoff_probability >= 0 AND handoff_probability <= 1),
    entered_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    cooldown_until      TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

COMMENT ON TABLE camera_wake_state IS
    'Current Model 4 wake-up state per camera, mirrored from the in-process state machine on every transition.';

CREATE INDEX IF NOT EXISTS idx_camera_wake_state_active
    ON camera_wake_state (state, updated_at DESC)
    WHERE state <> 'PASSIVE';

-- ---------------------------------------------------------------------------
-- Table: threat_alerts
-- ---------------------------------------------------------------------------
-- Audit trail for every P0/P1 alert fanned out to the console. The dispatcher in
-- adapters/external_db_bridge.py is fire-and-forget over a WebSocket; this table
-- is the record that an alert was ever raised.
--
-- alert_id is a 32-character SHA-256 prefix produced by AlertDispatcher.build_alert,
-- NOT a UUID — hence VARCHAR, and hence the idempotent upsert on it.
CREATE TABLE IF NOT EXISTS threat_alerts (
    alert_id             VARCHAR(64) PRIMARY KEY,
    priority             VARCHAR(4) NOT NULL DEFAULT 'P1'
                             CHECK (priority IN ('P0', 'P1', 'P2', 'P3')),
    classification       VARCHAR(32) NOT NULL,
    plate_number         VARCHAR(24),
    camera_id            UUID,
    alert_geom           GEOMETRY(Point, 4326),
    detected_at_utc_ms   BIGINT NOT NULL,
    dispatched_at_utc_ms BIGINT NOT NULL,
    confidence           DOUBLE PRECISION NOT NULL DEFAULT 0.0
                             CHECK (confidence >= 0 AND confidence <= 1),
    vehicle              JSONB NOT NULL DEFAULT '{}'::jsonb,
    subject              JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence             JSONB NOT NULL DEFAULT '{}'::jsonb,
    schema_version       SMALLINT NOT NULL DEFAULT 1,
    received_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

COMMENT ON TABLE threat_alerts IS
    'Audit trail of P0/P1 threat alerts published to the operator console.';
COMMENT ON COLUMN threat_alerts.alert_id IS
    'Deterministic 32-char SHA-256 prefix from AlertDispatcher.build_alert; re-publishing the same detection is idempotent.';
COMMENT ON COLUMN threat_alerts.subject IS
    'Named-subject payload. Contains identity data sourced from an audited CCTNS lookup — restrict SELECT accordingly.';

CREATE INDEX IF NOT EXISTS idx_threat_alerts_detected_at
    ON threat_alerts (detected_at_utc_ms DESC);

CREATE INDEX IF NOT EXISTS idx_threat_alerts_camera
    ON threat_alerts (camera_id, detected_at_utc_ms DESC);

CREATE INDEX IF NOT EXISTS idx_threat_alerts_plate
    ON threat_alerts (plate_number)
    WHERE plate_number IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Table: relay_sessions
-- ---------------------------------------------------------------------------
-- Live-stream sessions brokered through the FastAPI WebRTC proxy to
-- cmd/stream_relay. The relay keeps sessions in process memory and reaps after
-- 30 s of silence, so this table is both the restart-survivable record and the
-- work list the API's keepalive task heartbeats from.
--
-- session_id is VARCHAR, not UUID: it is whatever identifier the relay returns.
CREATE TABLE IF NOT EXISTS relay_sessions (
    session_id        VARCHAR(64) PRIMARY KEY,
    camera_id         UUID NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    relay_addr        TEXT NOT NULL,
    state             VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
                          CHECK (state IN ('ACTIVE', 'CLOSED', 'FAILED')),
    started_at        TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    closed_at         TIMESTAMPTZ
);

COMMENT ON TABLE relay_sessions IS
    'RTSP-to-WebRTC relay sessions brokered by the API proxy; the keepalive task heartbeats every ACTIVE row.';
COMMENT ON COLUMN relay_sessions.relay_addr IS
    'Base URL of the stream_relay instance that owns this session, so a multi-relay deployment heartbeats the right host.';

CREATE INDEX IF NOT EXISTS idx_relay_sessions_active
    ON relay_sessions (last_heartbeat_at)
    WHERE state = 'ACTIVE';

CREATE INDEX IF NOT EXISTS idx_relay_sessions_camera
    ON relay_sessions (camera_id, started_at DESC);

COMMIT;
