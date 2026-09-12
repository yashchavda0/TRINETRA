-- ============================================================================
-- Gujarat Police CCTV Integration System (TRINETRA)
-- Migration 003 : asset metadata, identity/RBAC, audit trail, boundaries
--
-- Target platform : PostgreSQL 16 + PostGIS 3.4
-- Depends on      : 001_init_model1_registry.sql, 002_pipeline_state.sql
--
-- Apply with:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/003_asset_metadata_and_identity.sql
--
-- WHY THIS MIGRATION EXISTS
--
-- Three tender Model 1 requirements had no data model at all before this file:
--
--   1. Asset metadata. The registry stored where a camera is, not what it is.
--      No type, make, model, installation date, IP, resolution, codec, recorder
--      reference or retention policy. "Gap analysis for ageing infrastructure"
--      was not merely unimplemented, it was uncomputable: created_at is the
--      REGISTRATION instant, so a camera installed in 2016 and imported last
--      week reads as new.
--
--   2. Audit trail. No created_by, updated_by, updated_at, and no history. The
--      only record of a change was a log line naming which fields moved - not
--      their values, and not who moved them.
--
--   3. Identity. No users, no roles, no department scoping. Every endpoint was
--      anonymous, including the one that returns stream_url in bulk - a column
--      001 itself documents as "a credential-bearing secret".
--
-- Idempotent and transactional, like 001 and 002.
-- ============================================================================

BEGIN;

-- pg_trgm powers the registry search box: fuzzy matching on camera code, site
-- name, make and model. Without it, search is an unindexed sequential ILIKE.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- Identity and access control
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- TEXT rather than CITEXT: citext is a contrib extension the postgis image
    -- may not carry, and a unique index on lower(email) gives the same
    -- case-insensitive guarantee with no extra dependency.
    email         TEXT NOT NULL,
    full_name     TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role          VARCHAR(20) NOT NULL DEFAULT 'VIEWER'
                      CHECK (role IN ('SUPER_ADMIN', 'DEPT_ADMIN', 'OPERATOR', 'VIEWER')),
    -- NULL department means fleet-wide scope, which only SUPER_ADMIN may hold.
    department_id VARCHAR(32) REFERENCES departments(id) ON DELETE RESTRICT,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    last_login_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (role = 'SUPER_ADMIN' OR department_id IS NOT NULL)
);

COMMENT ON TABLE users IS
    'Console and API operators. Role plus department_id together define what a caller may see and change.';
COMMENT ON COLUMN users.role IS
    'SUPER_ADMIN: fleet-wide, manages users. DEPT_ADMIN: full control of one department. OPERATOR: view + stream + acknowledge alerts. VIEWER: read-only.';
COMMENT ON COLUMN users.department_id IS
    'Scope of every query this user makes. Enforced server-side in the SQL predicate, never trusted from the client.';
COMMENT ON COLUMN users.password_hash IS
    'bcrypt. Never log, never return through the API, never include in an audit payload.';

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_lower ON users (lower(email));
CREATE INDEX IF NOT EXISTS idx_users_department ON users (department_id) WHERE is_active;

-- Service-to-service credentials: the health poller, the ANPR service and the
-- Model 4 worker are not people and must not hold a human's password.
CREATE TABLE IF NOT EXISTS api_keys (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          TEXT NOT NULL,
    key_hash      TEXT NOT NULL UNIQUE,
    role          VARCHAR(20) NOT NULL DEFAULT 'OPERATOR'
                      CHECK (role IN ('SUPER_ADMIN', 'DEPT_ADMIN', 'OPERATOR', 'VIEWER')),
    department_id VARCHAR(32) REFERENCES departments(id) ON DELETE RESTRICT,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    expires_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    last_used_at  TIMESTAMPTZ
);

COMMENT ON TABLE api_keys IS
    'Non-human callers. Only the hash is stored; the plaintext key is shown once at creation and never again.';

-- ---------------------------------------------------------------------------
-- Administrative boundaries
-- ---------------------------------------------------------------------------
-- Gap analysis and density reporting need something to be "per area" of. Load
-- ward/zone/district polygons here; without them, coverage analysis can only
-- report on a bounding box rather than on a jurisdiction anyone recognises.
CREATE TABLE IF NOT EXISTS admin_boundaries (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       TEXT NOT NULL,
    level      VARCHAR(20) NOT NULL
                   CHECK (level IN ('STATE', 'DISTRICT', 'CITY', 'ZONE', 'WARD', 'BEAT')),
    parent_id  UUID REFERENCES admin_boundaries(id) ON DELETE SET NULL,
    geom       GEOMETRY(MultiPolygon, 4326) NOT NULL,
    area_sq_km DOUBLE PRECISION,
    population INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (level, name)
);

COMMENT ON TABLE admin_boundaries IS
    'Administrative polygons (ward/zone/district) that coverage and density reports aggregate over. WGS84.';

CREATE INDEX IF NOT EXISTS idx_admin_boundaries_geom ON admin_boundaries USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_admin_boundaries_level ON admin_boundaries (level);

-- ---------------------------------------------------------------------------
-- Camera asset metadata
-- ---------------------------------------------------------------------------
-- Added as nullable columns: 001 is already deployed and populated, and a NOT
-- NULL default would invent facts about cameras nobody has surveyed. An unknown
-- installation date must read as unknown, not as today.

ALTER TABLE cameras ADD COLUMN IF NOT EXISTS camera_type VARCHAR(20)
    CHECK (camera_type IS NULL OR camera_type IN
        ('FIXED', 'PTZ', 'DOME', 'BULLET', 'ANPR', 'THERMAL', 'PANORAMIC', 'OTHER'));
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS make TEXT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS model TEXT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS serial_number TEXT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS ip_address INET;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS resolution VARCHAR(20);
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS codec VARCHAR(20);
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS frame_rate SMALLINT
    CHECK (frame_rate IS NULL OR (frame_rate > 0 AND frame_rate <= 240));
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS installed_on DATE;

-- Ownership, distinct from department. 001 collapses every private operator
-- into a single PRIVATE department row, so two different private owners are
-- indistinguishable without this.
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS owner_org TEXT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS custodian_name TEXT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS custodian_contact TEXT;

-- Recording and storage, per the tender's "storage details".
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS nvr_reference TEXT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS nvr_channel VARCHAR(32);
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS retention_days SMALLINT
    CHECK (retention_days IS NULL OR retention_days >= 0);
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS recording_enabled BOOLEAN NOT NULL DEFAULT FALSE;

-- Human-readable place. Coordinates alone cannot be searched or grouped by an
-- operator who thinks in junctions and wards.
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS site_name TEXT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS address TEXT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS ward TEXT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS zone TEXT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS district TEXT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS boundary_id UUID
    REFERENCES admin_boundaries(id) ON DELETE SET NULL;

-- Connectivity is NOT lifecycle status. A camera can be ACTIVE in the asset
-- register and OFFLINE on the network; the existing four-value status enum
-- cannot express both at once, which is why this is a separate column.
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS connectivity_status VARCHAR(20)
    NOT NULL DEFAULT 'UNKNOWN'
    CHECK (connectivity_status IN ('ONLINE', 'OFFLINE', 'DEGRADED', 'UNKNOWN'));
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS connectivity_type VARCHAR(20)
    CHECK (connectivity_type IS NULL OR connectivity_type IN
        ('FIBRE', 'ETHERNET', 'WIFI', 'CELLULAR_4G', 'CELLULAR_5G', 'RF', 'OTHER'));

-- Maintenance, orthogonal to lifecycle for the same reason as connectivity: a
-- camera due for service is still ACTIVE and still streaming.
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS maintenance_state VARCHAR(20)
    NOT NULL DEFAULT 'OK'
    CHECK (maintenance_state IN ('OK', 'DUE', 'OVERDUE', 'IN_PROGRESS', 'FAULTY'));
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS last_serviced_on DATE;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS next_service_due DATE;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS work_order_ref TEXT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS notes TEXT;

-- Audit columns.
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS created_by UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS updated_by UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp();

COMMENT ON COLUMN cameras.camera_type IS
    'Physical camera class. ANPR here means a camera dedicated to plate capture, not merely one the ANPR service samples.';
COMMENT ON COLUMN cameras.installed_on IS
    'When the camera was physically installed. Distinct from created_at, which is when this row was written - ageing reports must use this column.';
COMMENT ON COLUMN cameras.connectivity_status IS
    'Network reachability, denormalised from camera_health_logs by the health poller. Independent of the lifecycle status column.';
COMMENT ON COLUMN cameras.maintenance_state IS
    'Service condition, independent of lifecycle status: a camera can be ACTIVE and OVERDUE simultaneously.';
COMMENT ON COLUMN cameras.retention_days IS
    'Recording retention policy for this camera, driving the Central VMS storage tiering job.';

CREATE INDEX IF NOT EXISTS idx_cameras_type ON cameras (camera_type);
CREATE INDEX IF NOT EXISTS idx_cameras_connectivity ON cameras (connectivity_status);
CREATE INDEX IF NOT EXISTS idx_cameras_maintenance ON cameras (maintenance_state)
    WHERE maintenance_state <> 'OK';
CREATE INDEX IF NOT EXISTS idx_cameras_installed_on ON cameras (installed_on);
CREATE INDEX IF NOT EXISTS idx_cameras_boundary ON cameras (boundary_id);
CREATE INDEX IF NOT EXISTS idx_cameras_ward ON cameras (ward);

-- Trigram indexes for the registry search box.
CREATE INDEX IF NOT EXISTS idx_cameras_code_trgm
    ON cameras USING GIN (global_camera_code gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_cameras_site_trgm
    ON cameras USING GIN (site_name gin_trgm_ops);

-- ---------------------------------------------------------------------------
-- Audit trail
-- ---------------------------------------------------------------------------
-- Written by a trigger rather than by application code: an audit log the
-- application can forget to write is not an audit log. Any path that reaches
-- the table - API, migration, manual psql - is recorded.
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    entity      VARCHAR(40) NOT NULL,
    entity_id   TEXT NOT NULL,
    action      VARCHAR(10) NOT NULL CHECK (action IN ('INSERT', 'UPDATE', 'DELETE')),
    actor_id    UUID REFERENCES users(id) ON DELETE SET NULL,
    actor_label TEXT,
    before      JSONB,
    after       JSONB,
    changed     TEXT[],
    at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

COMMENT ON TABLE audit_log IS
    'Immutable record of registry metadata changes. Populated by trigger so no write path can bypass it.';
COMMENT ON COLUMN audit_log.actor_id IS
    'Set from the app.actor_id session variable the API sets per request; NULL for changes made outside the application.';
COMMENT ON COLUMN audit_log.changed IS
    'Names of the columns whose values actually differ, so a diff view needs no client-side comparison.';

CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log (entity, entity_id, at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log (at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log (actor_id, at DESC);

-- The API sets `SET LOCAL app.actor_id` inside each write transaction; this
-- reads it back. current_setting(..., true) returns NULL rather than raising
-- when the variable is unset, which is what makes the trigger safe for psql.
CREATE OR REPLACE FUNCTION audit_actor_id() RETURNS UUID AS $$
DECLARE
    raw       TEXT := current_setting('app.actor_id', true);
    candidate UUID;
BEGIN
    IF raw IS NULL OR raw = '' THEN
        RETURN NULL;
    END IF;

    BEGIN
        candidate := raw::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN NULL;
    END;

    -- Resolve to NULL unless the user still exists. Returning an id that no
    -- longer has a users row would trip the audit_log foreign key and abort the
    -- business transaction that the audit was only meant to annotate - a
    -- deleted operator could otherwise make every camera edit fail. The raw
    -- value is preserved in actor_label, so provenance survives the user row.
    IF EXISTS (SELECT 1 FROM users WHERE id = candidate) THEN
        RETURN candidate;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql STABLE;

-- Columns that must never be copied into the audit trail. to_jsonb(NEW) would
-- otherwise write every user's bcrypt hash into a table built for wide reads,
-- turning an accountability feature into a credential store.
CREATE OR REPLACE FUNCTION audit_redact(payload JSONB) RETURNS JSONB AS $$
BEGIN
    IF payload IS NULL THEN
        RETURN NULL;
    END IF;
    RETURN payload - 'password_hash' - 'key_hash';
END;
$$ LANGUAGE plpgsql IMMUTABLE;

CREATE OR REPLACE FUNCTION audit_row_change() RETURNS TRIGGER AS $$
DECLARE
    before_json JSONB;
    after_json  JSONB;
    changed_cols TEXT[];
BEGIN
    IF TG_OP = 'INSERT' THEN
        after_json := to_jsonb(NEW);
        before_json := NULL;
        changed_cols := ARRAY(SELECT jsonb_object_keys(after_json));
    ELSIF TG_OP = 'UPDATE' THEN
        before_json := to_jsonb(OLD);
        after_json := to_jsonb(NEW);
        -- Only the keys whose values actually differ. An UPDATE that rewrites a
        -- row with identical values should not look like a change.
        changed_cols := ARRAY(
            SELECT key FROM jsonb_each(after_json)
            WHERE value IS DISTINCT FROM (before_json -> key)
        );
        IF changed_cols = ARRAY[]::TEXT[] THEN
            RETURN NEW;
        END IF;
    ELSE
        before_json := to_jsonb(OLD);
        after_json := NULL;
        changed_cols := ARRAY(SELECT jsonb_object_keys(before_json));
    END IF;

    INSERT INTO audit_log (entity, entity_id, action, actor_id, actor_label, before, after, changed)
    VALUES (
        TG_TABLE_NAME,
        COALESCE((after_json ->> 'id'), (before_json ->> 'id'), 'unknown'),
        TG_OP,
        audit_actor_id(),
        -- Free-text provenance that survives the user row being deleted, and
        -- carries service callers that have no users entry at all.
        NULLIF(current_setting('app.actor_label', true), ''),
        audit_redact(before_json),
        audit_redact(after_json),
        changed_cols
    );

    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_audit_cameras ON cameras;
CREATE TRIGGER trg_audit_cameras
    AFTER INSERT OR UPDATE OR DELETE ON cameras
    FOR EACH ROW EXECUTE FUNCTION audit_row_change();

DROP TRIGGER IF EXISTS trg_audit_users ON users;
CREATE TRIGGER trg_audit_users
    AFTER INSERT OR UPDATE OR DELETE ON users
    FOR EACH ROW EXECUTE FUNCTION audit_row_change();

-- Keep updated_at honest without relying on every caller to set it.
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_touch_cameras ON cameras;
CREATE TRIGGER trg_touch_cameras
    BEFORE UPDATE ON cameras
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

DROP TRIGGER IF EXISTS trg_touch_users ON users;
CREATE TRIGGER trg_touch_users
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- ---------------------------------------------------------------------------
-- Detections: promote the plate out of JSONB
-- ---------------------------------------------------------------------------
-- 002 stores plate reads only inside detections.attributes, and a plate reaches
-- threat_alerts only if it escalated. A plate read that matched nothing was
-- therefore unsearchable - which is exactly what Model 2's "searchable vehicle
-- movement records" requires.
ALTER TABLE detections ADD COLUMN IF NOT EXISTS plate_number VARCHAR(24);
ALTER TABLE detections ADD COLUMN IF NOT EXISTS plate_confidence REAL
    CHECK (plate_confidence IS NULL OR (plate_confidence >= 0 AND plate_confidence <= 1));
ALTER TABLE detections ADD COLUMN IF NOT EXISTS snapshot_uri TEXT;

COMMENT ON COLUMN detections.plate_number IS
    'Normalised ANPR reading, uppercase and separator-stripped. Indexed for vehicle-movement search.';

CREATE INDEX IF NOT EXISTS idx_detections_plate_time
    ON detections (plate_number, timestamp_utc_ms DESC)
    WHERE plate_number IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_detections_plate_trgm
    ON detections USING GIN (plate_number gin_trgm_ops);

-- ---------------------------------------------------------------------------
-- Watchlist - vehicles and subjects of interest
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS watchlist (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plate_number  VARCHAR(24),
    target_id     TEXT,
    classification VARCHAR(32) NOT NULL DEFAULT 'PERSON_OF_INTEREST',
    priority      VARCHAR(4) NOT NULL DEFAULT 'P1'
                      CHECK (priority IN ('P0', 'P1', 'P2', 'P3')),
    reason        TEXT NOT NULL,
    case_reference TEXT,
    department_id VARCHAR(32) REFERENCES departments(id) ON DELETE SET NULL,
    added_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    expires_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    -- A watchlist entry that identifies nothing cannot match anything.
    CHECK (plate_number IS NOT NULL OR target_id IS NOT NULL)
);

COMMENT ON TABLE watchlist IS
    'Vehicles and re-identification targets of interest. The worker matches detections against active entries and raises alerts.';

CREATE UNIQUE INDEX IF NOT EXISTS idx_watchlist_plate_active
    ON watchlist (plate_number) WHERE is_active AND plate_number IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_watchlist_active ON watchlist (is_active, priority);

-- ---------------------------------------------------------------------------
-- Event tagging - operator annotations on detections and alerts
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event_tags (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity     VARCHAR(20) NOT NULL CHECK (entity IN ('DETECTION', 'ALERT', 'CAMERA')),
    entity_id  TEXT NOT NULL,
    tag        VARCHAR(64) NOT NULL,
    note       TEXT,
    tagged_by  UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (entity, entity_id, tag)
);

COMMENT ON TABLE event_tags IS
    'Operator-applied labels on detections, alerts and cameras. Model 2 event tagging and camera-wise indexing.';

CREATE INDEX IF NOT EXISTS idx_event_tags_lookup ON event_tags (entity, entity_id);
CREATE INDEX IF NOT EXISTS idx_event_tags_tag ON event_tags (tag, created_at DESC);

-- ---------------------------------------------------------------------------
-- Alert workflow - acknowledge and assign
-- ---------------------------------------------------------------------------
ALTER TABLE threat_alerts ADD COLUMN IF NOT EXISTS acknowledged_by UUID
    REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE threat_alerts ADD COLUMN IF NOT EXISTS acknowledged_at TIMESTAMPTZ;
ALTER TABLE threat_alerts ADD COLUMN IF NOT EXISTS assigned_to UUID
    REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE threat_alerts ADD COLUMN IF NOT EXISTS workflow_state VARCHAR(20)
    NOT NULL DEFAULT 'NEW'
    CHECK (workflow_state IN ('NEW', 'ACKNOWLEDGED', 'IN_PROGRESS', 'RESOLVED', 'FALSE_POSITIVE'));
ALTER TABLE threat_alerts ADD COLUMN IF NOT EXISTS resolution_note TEXT;

CREATE INDEX IF NOT EXISTS idx_threat_alerts_workflow
    ON threat_alerts (workflow_state, detected_at_utc_ms DESC);

COMMIT;
