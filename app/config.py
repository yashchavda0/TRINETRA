"""Runtime configuration for the Model 1 Camera Registry service.

Every value is sourced from the process environment (or a local ``.env`` file
during development) through pydantic-settings, so no credential is ever written
into the repository.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Service configuration.

    Environment variable names are the field names upper-cased, e.g.
    ``DATABASE_URL``, ``DB_POOL_MAX_SIZE``, ``CORS_ORIGINS``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ------------------------------------------------------
    app_name: str = "Model 1 - Central GIS Camera Registry"
    app_version: str = "1.0.0"
    environment: str = "development"
    log_level: str = "INFO"

    # --- Database ---------------------------------------------------------
    # Required. libpq-style DSN, e.g.
    #   postgresql://registry:secret@10.0.0.14:5432/gujcctv
    database_url: str = Field(...)

    # Pool sized for health-ping bursts across an 80,000+ camera fleet. Keep
    # max_size * replica_count below the server's max_connections.
    db_pool_min_size: int = Field(default=10, ge=1, le=1000)
    db_pool_max_size: int = Field(default=50, ge=1, le=1000)

    # Seconds a connection may sit idle before the pool recycles it, and the
    # ceiling on how long any single statement may run.
    db_pool_max_inactive_connection_lifetime: float = Field(default=300.0, gt=0)
    db_command_timeout: float = Field(default=30.0, gt=0)

    # --- HTTP -------------------------------------------------------------
    # Comma-separated: CORS_ORIGINS=https://gis.gujpolice.in,https://ops.gujpolice.in
    #
    # Typed as a plain string on purpose. pydantic-settings treats a list-typed
    # field as "complex" and JSON-decodes the raw environment value before any
    # field_validator can run, so a comma-separated value raises SettingsError at
    # startup. Callers read the parsed list from cors_origin_list below.
    cors_origins: str = "*"

    # Hard ceiling on rows returned by a single spatial search, so one wide
    # query cannot pull the whole fleet through the API.
    max_spatial_results: int = Field(default=500, ge=1, le=10_000)

    # Hard ceiling on health pings accepted in one bulk request.
    max_health_ping_batch: int = Field(default=5_000, ge=1, le=100_000)

    # Hard ceiling on rows returned by the camera list endpoint. Larger than the
    # spatial ceiling because the GIS console loads the whole visible fleet once.
    max_list_results: int = Field(default=10_000, ge=1, le=100_000)

    # --- Analytics bus (Kafka) -------------------------------------------
    # Topic and bootstrap must match cmd/ingestion_gateway, which publishes raw
    # SurveillanceEvent bytes keyed by the numeric DepartmentCode tag.
    # 127.0.0.1 rather than localhost: on Windows "localhost" resolves to ::1
    # first, and the broker's published port is IPv4-only.
    kafka_bootstrap_servers: str = "127.0.0.1:9092"
    kafka_topic_raw: str = "surveillance-events-raw"
    kafka_consumer_group: str = "trinetra-handoff-worker"
    kafka_poll_timeout_seconds: float = Field(default=1.0, gt=0)

    # --- Model 4 engine (engine/) ----------------------------------------
    # These override the module-level constants the engine hardcodes today.
    engine_device: str | None = None  # None -> CUDA when present, else CPU
    similarity_threshold: float = Field(default=0.85, ge=-1.0, le=1.0)
    handoff_probability_threshold: float = Field(default=0.65, gt=0.0, le=1.0)
    cooldown_seconds: float = Field(default=60.0, gt=0)
    pre_activation_timeout_seconds: float = Field(default=30.0, gt=0)
    edge_distance_threshold_m: float = Field(default=1_500.0, gt=0)
    max_active_targets: int = Field(default=4096, ge=1)
    target_ttl_seconds: float = Field(default=300.0, gt=0)
    scheduler_interval_seconds: float = Field(default=1.0, gt=0)
    graph_reload_seconds: float = Field(default=300.0, gt=0)

    # --- External state registries (adapters/) ---------------------------
    # Leaving a base URL empty puts that adapter in deterministic simulation
    # mode rather than failing - see _AsyncRegistryClient._simulate.
    egujcop_base_url: str | None = None
    egujcop_api_key: str | None = None
    vahan_base_url: str | None = None
    vahan_api_key: str | None = None

    # --- P0 alert channel -------------------------------------------------
    # Point this at this service's own fan-out socket so alerts reach the
    # console instead of the unresolvable ws://central-command default baked
    # into AlertDispatcher.
    p0_alert_ws_url: str = "ws://127.0.0.1:8000/alerts/p0"
    p0_alert_api_key: str | None = None
    alert_publish_url: str = "http://127.0.0.1:8000/api/v1/alerts/publish"

    # --- Media plane (MediaMTX) ------------------------------------------
    # MediaMTX carries the actual video: RTSP in, WebRTC out. The API proxies
    # the browser's SDP offer to its WHEP endpoint. Empty disables the backend.
    mediamtx_whep_base_url: str | None = "http://127.0.0.1:8889"
    mediamtx_api_base_url: str | None = "http://127.0.0.1:9997"
    # host:port clients use to reach MediaMTX's RTSP ingest. A camera whose
    # stream_url points here is already published; anything else is pulled
    # on demand through the control API.
    mediamtx_rtsp_host: str = "127.0.0.1:8554"
    mediamtx_request_timeout_seconds: float = Field(default=10.0, gt=0)
    # Separate, longer budget for the WHEP exchange on an on-demand pull path:
    # MediaMTX must dial the camera, negotiate RTSP and produce a keyframe
    # before it can answer. Reusing the control-API timeout here makes a slow
    # but working camera fail as though it were broken.
    mediamtx_source_start_timeout_seconds: float = Field(default=20.0, gt=0)

    # --- Stream relay (cmd/stream_relay) ---------------------------------
    # Empty means "relay not deployed": the WebRTC proxy then returns 503 and
    # the rest of the console keeps working.
    stream_relay_base_url: str | None = None
    relay_client_cert_file: str | None = None
    relay_client_key_file: str | None = None
    relay_ca_file: str | None = None
    relay_request_timeout_seconds: float = Field(default=10.0, gt=0)
    relay_session_keepalive_seconds: float = Field(default=10.0, gt=0)
    relay_session_max_age_seconds: float = Field(default=1_800.0, gt=0)

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        allowed = ("postgresql://", "postgres://", "postgresql+asyncpg://")
        if not value.startswith(allowed):
            raise ValueError(
                "database_url must be a PostgreSQL DSN starting with "
                "postgresql:// or postgres://"
            )
        # asyncpg takes a plain libpq DSN; strip a SQLAlchemy-style driver tag
        # if one was copied across from another service's configuration.
        return value.replace("postgresql+asyncpg://", "postgresql://", 1)

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}:
            raise ValueError(f"unsupported log_level: {value}")
        return level

    def model_post_init(self, __context: object) -> None:
        if self.db_pool_max_size < self.db_pool_min_size:
            raise ValueError(
                "db_pool_max_size must be greater than or equal to db_pool_min_size"
            )

    @property
    def safe_database_target(self) -> str:
        """Host/database portion of the DSN, with credentials removed.

        Used in logs and error responses so a connection failure never leaks a
        password.
        """
        dsn = self.database_url
        without_scheme = dsn.split("://", 1)[-1]
        return without_scheme.split("@", 1)[-1] if "@" in without_scheme else without_scheme


    @property
    def cors_origin_list(self) -> list[str]:
        """CORS_ORIGINS split into the list the CORS middleware expects."""
        origins = [item.strip() for item in self.cors_origins.split(",") if item.strip()]
        return origins or ["*"]

    @property
    def relay_configured(self) -> bool:
        """True when the WebRTC proxy has a relay to talk to."""
        return bool(self.stream_relay_base_url)

    @property
    def mediamtx_configured(self) -> bool:
        """True when MediaMTX is available to carry media."""
        return bool(self.mediamtx_whep_base_url)

    @property
    def media_backend(self) -> str:
        """Which backend serves live video: 'mediamtx', 'relay' or 'none'.

        MediaMTX wins when both are set: it is the only one of the two that
        implements ICE, DTLS and SRTP, so it is the only one that can deliver
        pictures to a browser.
        """
        if self.mediamtx_configured:
            return "mediamtx"
        if self.relay_configured:
            return "relay"
        return "none"

    @property
    def relay_client_cert(self) -> tuple[str, str] | None:
        """httpx client-certificate pair for the relay's mandatory mTLS, if set."""
        if self.relay_client_cert_file and self.relay_client_key_file:
            return (self.relay_client_cert_file, self.relay_client_key_file)
        return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings object, built once."""
    return Settings()
