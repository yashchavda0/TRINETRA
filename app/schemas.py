"""Pydantic v2 request/response contracts for the Model 1 Camera Registry.

Coordinate convention used by every schema here: ``latitude`` then
``longitude``, WGS84 decimal degrees (EPSG:4326). PostGIS takes the opposite
order, so the SQL layer binds ``ST_MakePoint(longitude, latitude)``. The swap
happens exactly once, in :mod:`app.routers.cameras`.
"""

from __future__ import annotations

from datetime import date, datetime
from ipaddress import ip_address
from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Shared constrained aliases ------------------------------------------------
Latitude = Annotated[float, Field(ge=-90.0, le=90.0, description="WGS84 latitude in decimal degrees")]
Longitude = Annotated[float, Field(ge=-180.0, le=180.0, description="WGS84 longitude in decimal degrees")]
Azimuth = Annotated[float, Field(ge=0.0, le=360.0, description="Compass bearing in degrees, 0 = true north")]
FieldOfView = Annotated[float, Field(gt=0.0, le=360.0, description="Horizontal field of view in degrees")]

CameraStatus = Literal["ACTIVE", "INACTIVE", "MAINTENANCE", "DECOMMISSIONED"]

# Lifecycle, connectivity and maintenance are three independent axes. A camera
# can be ACTIVE (asset register), OFFLINE (network) and OVERDUE (service) at the
# same time, which a single status column could never express.
CameraType = Literal[
    "FIXED", "PTZ", "DOME", "BULLET", "ANPR", "THERMAL", "PANORAMIC", "OTHER"
]
ConnectivityStatus = Literal["ONLINE", "OFFLINE", "DEGRADED", "UNKNOWN"]
ConnectivityType = Literal[
    "FIBRE", "ETHERNET", "WIFI", "CELLULAR_4G", "CELLULAR_5G", "RF", "OTHER"
]
MaintenanceState = Literal["OK", "DUE", "OVERDUE", "IN_PROGRESS", "FAULTY"]

_ALLOWED_STREAM_SCHEMES = frozenset({"rtsp", "rtsps", "http", "https"})


class CameraAssetFields(BaseModel):
    """Asset metadata shared by the create, update and read models.

    Every field is optional: these columns were added to a registry that was
    already populated, and inventing a value for a camera nobody has surveyed
    would be worse than recording that it is unknown. In particular
    ``installed_on`` must stay empty rather than defaulting to today, or every
    ageing-infrastructure report is wrong by the life of the camera.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    camera_type: CameraType | None = None
    make: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    serial_number: str | None = Field(default=None, max_length=120)
    ip_address: str | None = Field(default=None, max_length=45)
    resolution: str | None = Field(default=None, max_length=20, description="e.g. 1920x1080")
    codec: str | None = Field(default=None, max_length=20, description="e.g. H264, H265")
    frame_rate: int | None = Field(default=None, gt=0, le=240)
    installed_on: date | None = Field(
        default=None, description="Physical installation date, not registration date"
    )

    owner_org: str | None = Field(default=None, max_length=200)
    custodian_name: str | None = Field(default=None, max_length=200)
    custodian_contact: str | None = Field(default=None, max_length=200)

    nvr_reference: str | None = Field(default=None, max_length=200)
    nvr_channel: str | None = Field(default=None, max_length=32)
    retention_days: int | None = Field(default=None, ge=0, le=3650)
    recording_enabled: bool | None = None

    site_name: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=500)
    ward: str | None = Field(default=None, max_length=120)
    zone: str | None = Field(default=None, max_length=120)
    district: str | None = Field(default=None, max_length=120)

    connectivity_type: ConnectivityType | None = None
    maintenance_state: MaintenanceState | None = None
    last_serviced_on: date | None = None
    next_service_due: date | None = None
    work_order_ref: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("ip_address")
    @classmethod
    def _validate_ip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            # Postgres stores this as INET and would reject a malformed value
            # with a 500; catching it here produces a field-level 422 instead.
            ip_address(value)
        except ValueError as exc:
            raise ValueError(f"not a valid IP address: {value}") from exc
        return value

    @field_validator("camera_type", "codec", mode="before")
    @classmethod
    def _upper_enum(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _service_dates_ordered(self) -> "CameraAssetFields":
        if (
            self.last_serviced_on is not None
            and self.next_service_due is not None
            and self.next_service_due < self.last_serviced_on
        ):
            raise ValueError("next_service_due cannot be earlier than last_serviced_on")
        return self


def _check_stream_url(value: str) -> str:
    """Reject anything MediaMTX could not dial, before it reaches the registry.

    Shared by create and update so a camera cannot be edited into a state it
    could never have been registered in.
    """
    parsed = urlparse(value)
    if parsed.scheme.lower() not in _ALLOWED_STREAM_SCHEMES:
        raise ValueError(
            "stream_url scheme must be one of: " + ", ".join(sorted(_ALLOWED_STREAM_SCHEMES))
        )
    if not parsed.netloc:
        raise ValueError("stream_url must include a host, e.g. rtsp://10.2.3.4:554/stream1")
    return value


class CameraCreate(CameraAssetFields):
    """Payload for registering one camera in the Model 1 registry."""

    global_camera_code: str = Field(
        min_length=1,
        max_length=64,
        description="Fleet-wide unique human-readable code, e.g. AHM-TRF-00147",
    )
    department_id: str = Field(
        min_length=1,
        max_length=32,
        description="Owning department code, must already exist in departments",
    )
    latitude: Latitude
    longitude: Longitude
    azimuth_angle: Azimuth | None = Field(
        default=None, description="Bearing the camera faces; omit if unsurveyed"
    )
    fov_degrees: FieldOfView = Field(default=70.0)
    stream_url: str = Field(
        min_length=1,
        description="RTSP/RTSPS live stream endpoint or ONVIF device service URL",
    )
    vms_vendor: str | None = Field(default=None, max_length=64)
    status: CameraStatus = "ACTIVE"

    @field_validator("global_camera_code", "department_id")
    @classmethod
    def _upper_code(cls, value: str) -> str:
        return value.upper()

    @field_validator("stream_url")
    @classmethod
    def _validate_stream_url(cls, value: str) -> str:
        return _check_stream_url(value)


class CameraUpdate(CameraAssetFields):
    """Partial update of one registry row.

    Every field is optional and an omitted field is left untouched, which is
    what makes this safe to call from a correction script that only knows about
    coordinates. There is deliberately no way to *clear* a value: omission and
    null would otherwise be indistinguishable, and a null azimuth written by
    accident looks exactly like a camera that was never surveyed.

    ``global_camera_code`` and ``department_id`` are not updatable. The code is
    the fleet-wide identity other systems join on, and moving a camera between
    departments is an ownership transfer rather than a metadata edit.
    """

    latitude: Latitude | None = None
    longitude: Longitude | None = None
    azimuth_angle: Azimuth | None = None
    fov_degrees: FieldOfView | None = None
    stream_url: str | None = Field(default=None, min_length=1)
    vms_vendor: str | None = Field(default=None, max_length=64)
    status: CameraStatus | None = None

    @field_validator("stream_url")
    @classmethod
    def _validate_stream_url(cls, value: str) -> str:
        return _check_stream_url(value)

    @model_validator(mode="after")
    def _require_something_to_do(self) -> "CameraUpdate":
        if not self.model_fields_set:
            raise ValueError("no fields to update")
        # A half-specified position cannot be plotted and must not be stored as
        # a point at (0, lat) or (lon, 0) - the same rule the alert channel
        # enforces, for the same reason.
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be supplied together")
        return self


class CameraRead(BaseModel):
    """A registry row as returned by the API.

    ``location_geom`` is projected back into scalar ``latitude``/``longitude``
    by the SQL layer, so clients never handle WKB.

    ``stream_url`` reads ``[redacted]`` for anyone below DEPT_ADMIN: an RTSP URL
    routinely embeds camera credentials, and an operator can watch a camera
    without ever holding them.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    global_camera_code: str
    department_id: str | None
    latitude: float
    longitude: float
    azimuth_angle: float | None
    fov_degrees: float | None
    stream_url: str
    vms_vendor: str | None
    status: str
    created_at: datetime

    # --- asset metadata (all nullable; see CameraAssetFields) --------------
    camera_type: str | None = None
    make: str | None = None
    model: str | None = None
    serial_number: str | None = None
    ip_address: str | None = None
    resolution: str | None = None
    codec: str | None = None
    frame_rate: int | None = None
    installed_on: date | None = None
    owner_org: str | None = None
    custodian_name: str | None = None
    custodian_contact: str | None = None
    nvr_reference: str | None = None
    nvr_channel: str | None = None
    retention_days: int | None = None
    recording_enabled: bool | None = None
    site_name: str | None = None
    address: str | None = None
    ward: str | None = None
    zone: str | None = None
    district: str | None = None
    connectivity_status: str | None = None
    connectivity_type: str | None = None
    last_seen_at: datetime | None = None
    maintenance_state: str | None = None
    last_serviced_on: date | None = None
    next_service_due: date | None = None
    work_order_ref: str | None = None
    notes: str | None = None
    updated_at: datetime | None = None

    # Populated only by spatial search; None on direct reads and on create.
    distance_meters: float | None = None

    @field_validator("ip_address", mode="before")
    @classmethod
    def _stringify_inet(cls, value: object) -> object:
        # asyncpg returns INET as an ipaddress object, which pydantic would
        # reject for a str field.
        return str(value) if value is not None else None


class SpatialSearchQuery(BaseModel):
    """Validated parameters of a metre-accurate radius search."""

    model_config = ConfigDict(extra="forbid")

    latitude: Latitude
    longitude: Longitude
    radius_meters: float = Field(
        gt=0.0,
        le=50_000.0,
        description="Search radius in true metres, computed on the geography cast",
    )
    limit: int = Field(default=100, ge=1, le=10_000)
    department_id: str | None = Field(default=None, max_length=32)
    status: CameraStatus | None = None

    @field_validator("department_id")
    @classmethod
    def _upper_department(cls, value: str | None) -> str | None:
        return value.upper() if value else None


class BoundingBoxQuery(BaseModel):
    """Viewport (bounding box) query contract for the GIS map client.

    Declared here as the frozen request shape for envelope searches
    (``ST_MakeEnvelope(min_lon, min_lat, max_lon, max_lat, 4326)``). No route
    consumes it in this release: the registry ships with the three specified
    endpoints only, and the map viewport endpoint is scheduled separately.
    """

    model_config = ConfigDict(extra="forbid")

    min_lat: Latitude
    min_lon: Longitude
    max_lat: Latitude
    max_lon: Longitude
    limit: int = Field(default=1_000, ge=1, le=10_000)
    department_id: str | None = Field(default=None, max_length=32)
    status: CameraStatus | None = None

    @model_validator(mode="after")
    def _validate_envelope(self) -> "BoundingBoxQuery":
        if self.min_lat >= self.max_lat:
            raise ValueError("min_lat must be strictly less than max_lat")
        if self.min_lon >= self.max_lon:
            raise ValueError("min_lon must be strictly less than max_lon")
        return self


class HealthPing(BaseModel):
    """One camera reachability check result."""

    model_config = ConfigDict(extra="forbid")

    camera_id: UUID
    ping_latency_ms: int | None = Field(
        default=None,
        ge=0,
        le=600_000,
        description="Round-trip latency in milliseconds; null when unreachable",
    )
    is_reachable: bool

    @model_validator(mode="after")
    def _drop_latency_when_unreachable(self) -> "HealthPing":
        # An unreachable camera has no round-trip time. Storing a latency
        # alongside is_reachable=false would poison availability reporting.
        if not self.is_reachable and self.ping_latency_ms is not None:
            raise ValueError("ping_latency_ms must be null when is_reachable is false")
        return self


class HealthPingBatch(BaseModel):
    """Bulk submission of health checks from a fleet poller."""

    model_config = ConfigDict(extra="forbid")

    pings: list[HealthPing] = Field(min_length=1, max_length=5_000)


class HealthPingResult(BaseModel):
    """Outcome of a bulk health-ping insert."""

    inserted: int = Field(ge=0, description="Rows written to camera_health_logs")


class HealthResponse(BaseModel):
    """Service liveness/readiness payload."""

    status: Literal["ok", "degraded"]
    service: str
    version: str
    database: Literal["up", "down"]
    timestamp_utc_ms: int = Field(description="Server time as UTC Unix epoch milliseconds")


class CameraListResponse(BaseModel):
    """Paginated camera listing.

    The GIS console accepts either this envelope or a bare array; the envelope is
    used so the client can show a fleet total without a second request.
    """

    items: list[CameraRead]
    total: int = Field(ge=0, description="Rows matching the filter, ignoring limit/offset")
    limit: int
    offset: int


class CameraHealthRead(BaseModel):
    """Latest reachability state of one camera.

    Field names match what the console's camera popup reads.
    """

    camera_id: UUID
    status: Literal["UP", "DOWN", "UNKNOWN"]
    is_reachable: bool | None = None
    ping_latency_ms: int | None = None
    last_ping_at: datetime | None = Field(
        default=None, description="logged_at of the most recent health row, UTC"
    )


class WebRTCOfferRequest(BaseModel):
    """SDP offer from the console's RTCPeerConnection.

    Note there is no rtsp_url field: the stream URL is resolved server-side from
    the registry. Accepting a caller-supplied RTSP target would let any console
    user point the relay at an arbitrary host.
    """

    model_config = ConfigDict(extra="ignore")

    camera_id: UUID
    sdp: str = Field(min_length=1)
    type: str = Field(default="offer")

    @field_validator("sdp")
    @classmethod
    def _validate_sdp(cls, value: str) -> str:
        if "v=0" not in value:
            raise ValueError("sdp does not look like a session description (no v=0 line)")
        return value


class WebRTCAnswerResponse(BaseModel):
    """SDP answer relayed back to the browser."""

    type: Literal["answer"] = "answer"
    sdp: str
    session_id: str
    camera_id: UUID


class StreamStateResponse(BaseModel):
    """What the media server currently holds for one camera.

    The console asks for this right after a stream is negotiated, because the
    answer SDP does not say which codec is actually arriving and a codec the
    browser cannot decode is indistinguishable from a dead camera: both are a
    black tile. ``tracks`` is MediaMTX's own list, so ``["H265"]`` is the whole
    explanation for a picture that never appears in Chrome.

    Deliberately carries no URL of any kind. A camera's ``stream_url`` may hold
    credentials, which is why even error text goes through
    ``_rtsp_endpoint``.
    """

    camera_id: UUID
    path: str = Field(description="MediaMTX path name serving this camera")
    ready: bool = Field(description="False when the path does not exist or has no source yet")
    tracks: list[str] = Field(default_factory=list, description="Codecs MediaMTX sees, e.g. H264")
    ready_time: str | None = Field(default=None, description="When the source became ready, UTC")


class AlertPublishRequest(BaseModel):
    """A threat alert pushed in by the Model 4 worker for fan-out and audit.

    Mirrors ThreatAlert.to_dict() from adapters/external_db_bridge.py.
    """

    model_config = ConfigDict(extra="ignore")

    alert_id: str = Field(min_length=1, max_length=64)
    priority: Literal["P0", "P1", "P2", "P3"] = "P1"
    classification: str = Field(min_length=1, max_length=32)
    plate_number: str | None = Field(default=None, max_length=24)
    camera_id: UUID | None = None
    latitude: Latitude | None = None
    longitude: Longitude | None = None
    detected_at: int = Field(gt=0, description="UTC Unix epoch milliseconds")
    dispatched_at: int = Field(gt=0, description="UTC Unix epoch milliseconds")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    vehicle: dict[str, object] = Field(default_factory=dict)
    subject: dict[str, object] = Field(default_factory=dict)
    evidence: dict[str, object] = Field(default_factory=dict)
    schema_version: int = 1

    @model_validator(mode="after")
    def _require_full_position(self) -> "AlertPublishRequest":
        # A half-specified position cannot be plotted and must not be stored as
        # a point at (0, lat) or (lon, 0).
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be supplied together")
        return self


class AlertPublishResult(BaseModel):
    """Outcome of an alert publish."""

    alert_id: str
    stored: bool = Field(description="False when this alert_id was already recorded")
    subscribers_notified: int = Field(ge=0)


# ---------------------------------------------------------------------------
# Registry reports (Model 1 gap analysis)
# ---------------------------------------------------------------------------


class CoverageSummary(BaseModel):
    """Coverage of one administrative area."""

    boundary_id: str
    name: str
    level: str
    area_sq_km: float
    covered_sq_km: float
    coverage_percent: float = Field(ge=0, le=100)
    camera_count: int = Field(ge=0)


class CoverageReport(BaseModel):
    """Fleet viewshed union plus a per-boundary breakdown."""

    range_meters: float
    camera_count: int = Field(ge=0)
    covered_sq_km: float
    boundaries: list[CoverageSummary] = Field(default_factory=list)
    # GeoJSON string, passed straight to the map layer without re-parsing.
    footprint_geojson: str | None = None
    caveat: str


class DensityRow(BaseModel):
    boundary_id: str
    name: str
    level: str
    area_sq_km: float
    camera_count: int = Field(ge=0)
    cameras_per_sq_km: float
    population: int | None = None


class AgeingBand(BaseModel):
    band: str
    camera_count: int = Field(ge=0)


class AgeingReport(BaseModel):
    replacement_years: int
    due_for_replacement: int = Field(ge=0)
    # Surfaced rather than hidden: a large unknown count means the report
    # understates the problem, and the operator needs to know that.
    unknown_installation_date: int = Field(ge=0)
    bands: list[AgeingBand] = Field(default_factory=list)


class MaintenanceDueRow(BaseModel):
    camera_id: str
    global_camera_code: str
    site_name: str | None = None
    maintenance_state: str | None = None
    next_service_due: date | None = None
    work_order_ref: str | None = None


class FleetHealthSummary(BaseModel):
    """Fleet rollup for the health dashboard."""

    window_hours: int
    total_cameras: int = Field(ge=0)
    up: int = Field(ge=0)
    down: int = Field(ge=0)
    unknown: int = Field(ge=0)
    # None when no ping landed in the window: 0% would read as a total outage.
    uptime_percent: float | None = None
    pings_in_window: int = Field(ge=0)
    maintenance_attention: int = Field(ge=0)
    maintenance_due: list[MaintenanceDueRow] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Bulk import / export
# ---------------------------------------------------------------------------


class BulkRowError(BaseModel):
    """One rejected row of an import file."""

    row_number: int = Field(description="1-based row number in the source file")
    global_camera_code: str | None = None
    errors: list[str]


class BulkImportResult(BaseModel):
    """Outcome of a bulk import, in dry-run or committed form."""

    dry_run: bool
    total_rows: int = Field(ge=0)
    valid_rows: int = Field(ge=0)
    inserted: int = Field(ge=0)
    updated: int = Field(ge=0)
    skipped_existing: int = Field(ge=0)
    failed_rows: list[BulkRowError] = Field(default_factory=list)
    message: str


# ---------------------------------------------------------------------------
# Identity and access control
# ---------------------------------------------------------------------------

UserRole = Literal["SUPER_ADMIN", "DEPT_ADMIN", "OPERATOR", "VIEWER"]


class LoginRequest(BaseModel):
    """Credentials presented at the login endpoint."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)


class TokenResponse(BaseModel):
    """Issued token pair plus the profile the console needs to render itself."""

    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Access token lifetime in seconds")
    user: "UserRead"


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=1)


class UserCreate(BaseModel):
    """New console user. Only an admin may call the endpoint that takes this."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    email: str = Field(min_length=3, max_length=254)
    full_name: str = Field(min_length=1, max_length=200)
    # 72 bytes is bcrypt's hard truncation point; longer input is rejected
    # rather than silently shortened.
    password: str = Field(min_length=8, max_length=72)
    role: UserRole = "VIEWER"
    department_id: str | None = Field(default=None, max_length=32)

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        value = value.lower()
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            raise ValueError("email must contain a local part and a domain")
        return value

    @field_validator("department_id")
    @classmethod
    def _upper_department(cls, value: str | None) -> str | None:
        return value.upper() if value else None

    @model_validator(mode="after")
    def _scope_required_below_super_admin(self) -> "UserCreate":
        # A non-super-admin with no department has no scope at all, which the
        # database CHECK also refuses - catching it here gives a usable message.
        if self.role != "SUPER_ADMIN" and not self.department_id:
            raise ValueError("department_id is required for every role except SUPER_ADMIN")
        return self


class UserUpdate(BaseModel):
    """Partial update of a user. Omitted fields are left untouched."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    password: str | None = Field(default=None, min_length=8, max_length=72)
    role: UserRole | None = None
    department_id: str | None = Field(default=None, max_length=32)
    is_active: bool | None = None

    @field_validator("department_id")
    @classmethod
    def _upper_department(cls, value: str | None) -> str | None:
        return value.upper() if value else None

    @model_validator(mode="after")
    def _require_something_to_do(self) -> "UserUpdate":
        if not self.model_fields_set:
            raise ValueError("no fields to update")
        return self


class UserRead(BaseModel):
    """A user as returned by the API. Never carries the password hash."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    full_name: str
    role: UserRole
    department_id: str | None
    is_active: bool
    last_login_at: datetime | None = None
    created_at: datetime | None = None


class UserListResponse(BaseModel):
    items: list[UserRead]
    total: int = Field(ge=0)


class AuditEntry(BaseModel):
    """One row of the metadata audit trail."""

    id: int
    entity: str
    entity_id: str
    action: Literal["INSERT", "UPDATE", "DELETE"]
    actor_id: UUID | None = None
    actor_label: str | None = None
    changed: list[str] = Field(default_factory=list)
    before: dict[str, object] | None = None
    after: dict[str, object] | None = None
    at: datetime


class AuditListResponse(BaseModel):
    items: list[AuditEntry]
    total: int = Field(ge=0)
    limit: int
    offset: int


class ErrorResponse(BaseModel):
    """Uniform error envelope returned by every failing endpoint."""

    detail: str
    request_id: str | None = None
