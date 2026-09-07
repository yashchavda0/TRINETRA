"""Pydantic v2 request/response contracts for the Model 1 Camera Registry.

Coordinate convention used by every schema here: ``latitude`` then
``longitude``, WGS84 decimal degrees (EPSG:4326). PostGIS takes the opposite
order, so the SQL layer binds ``ST_MakePoint(longitude, latitude)``. The swap
happens exactly once, in :mod:`app.routers.cameras`.
"""

from __future__ import annotations

from datetime import datetime
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

_ALLOWED_STREAM_SCHEMES = frozenset({"rtsp", "rtsps", "http", "https"})


class CameraCreate(BaseModel):
    """Payload for registering one camera in the Model 1 registry."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

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
        parsed = urlparse(value)
        if parsed.scheme.lower() not in _ALLOWED_STREAM_SCHEMES:
            raise ValueError(
                "stream_url scheme must be one of: "
                + ", ".join(sorted(_ALLOWED_STREAM_SCHEMES))
            )
        if not parsed.netloc:
            raise ValueError("stream_url must include a host, e.g. rtsp://10.2.3.4:554/stream1")
        return value


class CameraRead(BaseModel):
    """A registry row as returned by the API.

    ``location_geom`` is projected back into scalar ``latitude``/``longitude``
    by the SQL layer, so clients never handle WKB.
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

    # Populated only by spatial search; None on direct reads and on create.
    distance_meters: float | None = None


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


class ErrorResponse(BaseModel):
    """Uniform error envelope returned by every failing endpoint."""

    detail: str
    request_id: str | None = None
