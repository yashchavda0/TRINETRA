"""External state-database integration adapters (eGujCop CCTNS, VAHAN)."""

from adapters.external_db_bridge import (
    AlertDispatcher,
    ExternalLookupError,
    MatchClassification,
    ThreatAlert,
    VahanAdapter,
    eGujCopAdapter,
    normalise_plate,
)

__all__ = [
    "AlertDispatcher",
    "ExternalLookupError",
    "MatchClassification",
    "ThreatAlert",
    "VahanAdapter",
    "eGujCopAdapter",
    "normalise_plate",
]
