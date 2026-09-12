"""External state-database integration adapters (eGujCop CCTNS, VAHAN)."""

from adapters.external_db_bridge import (
    AlertDispatcher,
    ExternalLookupError,
    MatchClassification,
    ThreatAlert,
    VahanAdapter,
    build_alert_id,
    eGujCopAdapter,
    normalise_plate,
    now_epoch_ms,
    screen_target,
)

__all__ = [
    "AlertDispatcher",
    "ExternalLookupError",
    "MatchClassification",
    "ThreatAlert",
    "VahanAdapter",
    "build_alert_id",
    "eGujCopAdapter",
    "normalise_plate",
    "now_epoch_ms",
    "screen_target",
]
