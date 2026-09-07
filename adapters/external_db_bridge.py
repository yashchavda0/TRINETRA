"""Asynchronous bridge between the surveillance pipeline and Gujarat state
record systems (eGujCop / CCTNS and VAHAN), plus the Priority 0 alert fan-out.

Design rules enforced here:

* **No blocking calls.** Every network hop is awaited; the whole module is safe
  to drive from the ingestion gateway's event loop at multi-thousand
  events-per-second.
* **No credentials in source.** Endpoints and API keys come from the process
  environment (``EGUJCOP_BASE_URL``, ``EGUJCOP_API_KEY``, ``VAHAN_BASE_URL``,
  ``VAHAN_API_KEY``, ``P0_ALERT_WS_URL``). With no base URL configured the
  adapter runs in *simulation* mode so the pipeline can be exercised without
  touching live state registries.
* **Fail soft.** A registry outage degrades to ``hit=False`` with an ``error``
  field; it never raises into the detection hot path unless the caller opts in
  via ``raise_on_error``.
* **Timestamps are UTC epoch milliseconds**, matching
  ``proto/surveillance_event.proto``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from types import TracebackType
from typing import Any, Final, Self

import httpx
import websockets

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants and helpers
# ---------------------------------------------------------------------------

# Indian registration marks, whitespace/hyphen stripped and upper-cased:
#   GJ01AB1234, GJ 1 A 1, DL3CAT4000, plus BH-series 22BH1234AA.
_PLATE_RE: Final = re.compile(r"^(?:[A-Z]{2}\d{1,2}[A-Z]{0,3}\d{1,4}|\d{2}BH\d{4}[A-Z]{1,2})$")
_PLATE_STRIP_RE: Final = re.compile(r"[\s\-.]")

_MAX_PERSON_ATTRIBUTES: Final = 32
_MAX_ATTRIBUTE_LEN: Final = 256


def now_epoch_ms() -> int:
    """Current UTC time as epoch milliseconds (the bus-wide time unit)."""
    return int(time.time() * 1000)


def normalise_plate(plate_number: str) -> str:
    """Upper-case and strip separators from a registration mark.

    Raises:
        ValueError: if the value is not a plausible Indian registration mark.
            Validating here keeps unsanitised operator input out of the URLs
            and query payloads sent to the state registries.
    """
    if not isinstance(plate_number, str):
        raise ValueError("plate_number must be a string")
    candidate = _PLATE_STRIP_RE.sub("", plate_number).upper()
    if not _PLATE_RE.match(candidate):
        raise ValueError(f"not a valid registration mark: {plate_number!r}")
    return candidate


def _sanitise_attributes(attributes: dict[str, Any] | None) -> dict[str, Any]:
    """Clamp the free-form person-attribute bag to a bounded, JSON-safe shape."""
    if not attributes:
        return {}
    clean: dict[str, Any] = {}
    for key, value in list(attributes.items())[:_MAX_PERSON_ATTRIBUTES]:
        if not isinstance(key, str):
            continue
        if isinstance(value, str):
            clean[key[:64]] = value[:_MAX_ATTRIBUTE_LEN]
        elif isinstance(value, (int, float, bool)) or value is None:
            clean[key[:64]] = value
    return clean


class MatchClassification(str, Enum):
    """Outcome classes returned by :class:`eGujCopAdapter`."""

    WANTED_CRIMINAL = "WANTED_CRIMINAL"
    STOLEN_VEHICLE = "STOLEN_VEHICLE"
    NONE = "NONE"


class ExternalLookupError(RuntimeError):
    """Raised when a registry lookup fails and the caller asked to see it."""


# ---------------------------------------------------------------------------
# Shared async HTTP plumbing
# ---------------------------------------------------------------------------


@dataclass
class _CacheEntry:
    value: dict[str, Any]
    expires_at: float


class _TTLCache:
    """Tiny bounded TTL cache; a convoy of vehicles re-queries the same plate."""

    def __init__(self, maxsize: int = 4096, ttl_seconds: float = 30.0) -> None:
        self._maxsize = maxsize
        self._ttl = ttl_seconds
        self._entries: dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> dict[str, Any] | None:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= time.monotonic():
                self._entries.pop(key, None)
                return None
            return dict(entry.value)

    async def set(self, key: str, value: dict[str, Any]) -> None:
        async with self._lock:
            if len(self._entries) >= self._maxsize:
                # Cheap eviction: drop the oldest-inserted quarter.
                for stale in list(self._entries)[: self._maxsize // 4 or 1]:
                    self._entries.pop(stale, None)
            self._entries[key] = _CacheEntry(dict(value), time.monotonic() + self._ttl)


class _AsyncRegistryClient:
    """Base class: pooled HTTP/2 client, retry with jittered backoff, bulkhead."""

    #: Overridden by subclasses; used for logging and simulation salting.
    registry_name: str = "registry"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        timeout_seconds: float = 1.5,
        max_retries: int = 2,
        max_concurrency: int = 64,
        cache_ttl_seconds: float = 30.0,
        simulate: bool | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._max_retries = max(0, max_retries)
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._cache = _TTLCache(ttl_seconds=cache_ttl_seconds)
        self._simulate = (not self._base_url) if simulate is None else simulate
        self._client = client
        self._owns_client = client is None
        self._client_lock = asyncio.Lock()

    @property
    def simulating(self) -> bool:
        return self._simulate

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    headers = {"Accept": "application/json"}
                    if self._api_key:
                        headers["Authorization"] = f"Bearer {self._api_key}"
                    self._client = httpx.AsyncClient(
                        base_url=self._base_url,
                        headers=headers,
                        timeout=httpx.Timeout(self._timeout),
                        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
                        http2=True,
                    )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST with bounded retries. Only idempotent lookup endpoints use this."""
        client = await self._get_client()
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                async with self._semaphore:
                    response = await client.post(path, json=payload)
                if response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"{self.registry_name} returned {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    raise ValueError(f"{self.registry_name} returned a non-object body")
                return body
            except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    break
                backoff = min(0.2 * (2**attempt), 1.0) * (0.5 + random.random())
                await asyncio.sleep(backoff)
        raise ExternalLookupError(f"{self.registry_name} lookup failed: {last_error}") from last_error

    def _deterministic_roll(self, *parts: str) -> float:
        """Stable pseudo-random in [0, 1) used only by simulation mode."""
        digest = hashlib.sha256("|".join((self.registry_name, *parts)).encode()).digest()
        return int.from_bytes(digest[:8], "big") / 2**64


# ---------------------------------------------------------------------------
# eGujCop / CCTNS
# ---------------------------------------------------------------------------


class eGujCopAdapter(_AsyncRegistryClient):
    """Warrant and stolen-property lookups against the eGujCop CCTNS registry."""

    registry_name = "eGujCop"

    def __init__(self, base_url: str | None = None, api_key: str | None = None, **kwargs: Any) -> None:
        super().__init__(
            base_url if base_url is not None else os.getenv("EGUJCOP_BASE_URL"),
            api_key if api_key is not None else os.getenv("EGUJCOP_API_KEY"),
            **kwargs,
        )

    async def check_warrants_and_stolen(
        self,
        plate_number: str,
        person_attributes: dict[str, Any] | None = None,
        *,
        raise_on_error: bool = False,
    ) -> dict[str, Any]:
        """Look a target up in CCTNS.

        Args:
            plate_number: Registration mark read by the ANPR stage.
            person_attributes: Soft biometrics / appearance descriptors from the
                re-identification stage (``gender``, ``upper_wear_colour``,
                ``face_embedding_id``, ...). Bounded and coerced before use.
            raise_on_error: Propagate registry failures instead of returning a
                soft miss. Off by default so the detection loop never stalls.

        Returns:
            ``{"hit", "classification", "confidence", "plate_number",
            "subject_name", "case_references", "source", "checked_at",
            "latency_ms", "error"}``. ``classification`` is always one of
            ``WANTED_CRIMINAL`` / ``STOLEN_VEHICLE`` / ``NONE``.
        """
        started = time.perf_counter()
        attributes = _sanitise_attributes(person_attributes)
        try:
            plate = normalise_plate(plate_number)
        except ValueError as exc:
            if raise_on_error:
                raise
            return self._miss(plate_number, error=str(exc), started=started)

        cache_key = f"{plate}:{hash(frozenset(attributes.items()))}"
        cached = await self._cache.get(cache_key)
        if cached is not None:
            cached["cached"] = True
            return cached

        try:
            if self._simulate:
                raw = self._simulate_cctns(plate, attributes)
            else:
                raw = await self._post_json(
                    "/api/v1/cctns/lookup",
                    {"registration_mark": plate, "person_attributes": attributes},
                )
        except ExternalLookupError as exc:
            logger.warning("eGujCop lookup failed for %s: %s", plate, exc)
            if raise_on_error:
                raise
            return self._miss(plate, error=str(exc), started=started)

        result = self._normalise_cctns(plate, raw, started)
        await self._cache.set(cache_key, result)
        return result

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _classify(value: object) -> MatchClassification:
        try:
            return MatchClassification(str(value).upper())
        except ValueError:
            return MatchClassification.NONE

    def _normalise_cctns(self, plate: str, raw: dict[str, Any], started: float) -> dict[str, Any]:
        classification = self._classify(raw.get("classification", raw.get("match_type")))
        case_references = raw.get("case_references") or []
        if not isinstance(case_references, list):
            case_references = [str(case_references)]
        return {
            "hit": classification is not MatchClassification.NONE,
            "classification": classification.value,
            "confidence": float(raw.get("confidence", 0.0) or 0.0),
            "plate_number": plate,
            "subject_name": raw.get("subject_name"),
            "case_references": [str(ref)[:64] for ref in case_references[:10]],
            "source": "EGUJCOP_CCTNS",
            "simulated": self._simulate,
            "cached": False,
            "checked_at": now_epoch_ms(),
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": None,
        }

    def _miss(self, plate: str, *, error: str | None, started: float) -> dict[str, Any]:
        return {
            "hit": False,
            "classification": MatchClassification.NONE.value,
            "confidence": 0.0,
            "plate_number": plate,
            "subject_name": None,
            "case_references": [],
            "source": "EGUJCOP_CCTNS",
            "simulated": self._simulate,
            "cached": False,
            "checked_at": now_epoch_ms(),
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": error,
        }

    def _simulate_cctns(self, plate: str, attributes: dict[str, Any]) -> dict[str, Any]:
        roll = self._deterministic_roll(plate)
        if roll < 0.03:
            return {
                "classification": MatchClassification.WANTED_CRIMINAL.value,
                "confidence": 0.86 + roll,
                "subject_name": f"SUBJECT-{plate[-4:]}",
                "case_references": [f"FIR/{plate[:4]}/{2020 + int(roll * 5)}/00{int(roll * 900) % 10}"],
                "matched_attributes": sorted(attributes)[:5],
            }
        if roll < 0.09:
            return {
                "classification": MatchClassification.STOLEN_VEHICLE.value,
                "confidence": 0.90,
                "case_references": [f"FIR/THEFT/{plate[:4]}/{int(roll * 9000):04d}"],
            }
        return {"classification": MatchClassification.NONE.value, "confidence": 0.0}


# ---------------------------------------------------------------------------
# VAHAN
# ---------------------------------------------------------------------------

_SIM_MAKES: Final = ("MARUTI SUZUKI", "HYUNDAI", "TATA MOTORS", "MAHINDRA", "HONDA", "TOYOTA")
_SIM_COLOURS: Final = ("WHITE", "SILVER", "BLACK", "RED", "BLUE", "GREY")


class VahanAdapter(_AsyncRegistryClient):
    """Registration verification against the VAHAN national vehicle registry."""

    registry_name = "VAHAN"

    def __init__(self, base_url: str | None = None, api_key: str | None = None, **kwargs: Any) -> None:
        super().__init__(
            base_url if base_url is not None else os.getenv("VAHAN_BASE_URL"),
            api_key if api_key is not None else os.getenv("VAHAN_API_KEY"),
            cache_ttl_seconds=kwargs.pop("cache_ttl_seconds", 900.0),
            **kwargs,
        )

    async def verify_vehicle_registration(
        self, plate_number: str, *, raise_on_error: bool = False
    ) -> dict[str, Any]:
        """Resolve a registration mark to its VAHAN record.

        Returns:
            ``{"found", "registration_status", "make", "model", "colour",
            "fuel_type", "owner_name_masked", "registration_date",
            "insurance_valid", "fitness_valid", "blacklisted",
            "blacklist_reason", "source", "checked_at", "latency_ms", "error"}``.
            ``registration_status`` is ``ACTIVE`` / ``EXPIRED`` /
            ``SUSPENDED`` / ``SCRAPPED`` / ``UNKNOWN``.
        """
        started = time.perf_counter()
        try:
            plate = normalise_plate(plate_number)
        except ValueError as exc:
            if raise_on_error:
                raise
            return self._not_found(plate_number, error=str(exc), started=started)

        cached = await self._cache.get(plate)
        if cached is not None:
            cached["cached"] = True
            return cached

        try:
            if self._simulate:
                raw = self._simulate_vahan(plate)
            else:
                raw = await self._post_json("/api/v1/vahan/registration", {"registration_mark": plate})
        except ExternalLookupError as exc:
            logger.warning("VAHAN lookup failed for %s: %s", plate, exc)
            if raise_on_error:
                raise
            return self._not_found(plate, error=str(exc), started=started)

        result = self._normalise_vahan(plate, raw, started)
        await self._cache.set(plate, result)
        return result

    # -- internals ---------------------------------------------------------

    def _normalise_vahan(self, plate: str, raw: dict[str, Any], started: float) -> dict[str, Any]:
        status = str(raw.get("registration_status", "UNKNOWN")).upper()
        if status not in {"ACTIVE", "EXPIRED", "SUSPENDED", "SCRAPPED", "UNKNOWN"}:
            status = "UNKNOWN"
        return {
            "found": bool(raw.get("found", True)),
            "plate_number": plate,
            "registration_status": status,
            "make": raw.get("make"),
            "model": raw.get("model"),
            "colour": (raw.get("colour") or raw.get("color") or None),
            "fuel_type": raw.get("fuel_type"),
            # Never surface a full owner identity to the operator console; the
            # named record stays behind an audited CCTNS request.
            "owner_name_masked": _mask_name(raw.get("owner_name")),
            "registration_date": raw.get("registration_date"),
            "insurance_valid": raw.get("insurance_valid"),
            "fitness_valid": raw.get("fitness_valid"),
            "blacklisted": bool(raw.get("blacklisted", False)),
            "blacklist_reason": raw.get("blacklist_reason"),
            "source": "VAHAN",
            "simulated": self._simulate,
            "cached": False,
            "checked_at": now_epoch_ms(),
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": None,
        }

    def _not_found(self, plate: str, *, error: str | None, started: float) -> dict[str, Any]:
        return {
            "found": False,
            "plate_number": plate,
            "registration_status": "UNKNOWN",
            "make": None,
            "model": None,
            "colour": None,
            "fuel_type": None,
            "owner_name_masked": None,
            "registration_date": None,
            "insurance_valid": None,
            "fitness_valid": None,
            "blacklisted": False,
            "blacklist_reason": None,
            "source": "VAHAN",
            "simulated": self._simulate,
            "cached": False,
            "checked_at": now_epoch_ms(),
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": error,
        }

    def _simulate_vahan(self, plate: str) -> dict[str, Any]:
        roll = self._deterministic_roll(plate)
        blacklisted = roll > 0.94
        return {
            "found": roll > 0.02,
            "registration_status": "ACTIVE" if roll < 0.88 else "EXPIRED",
            "make": _SIM_MAKES[int(roll * len(_SIM_MAKES)) % len(_SIM_MAKES)],
            "model": f"MODEL-{int(roll * 97) % 97:02d}",
            "colour": _SIM_COLOURS[int(roll * 7919) % len(_SIM_COLOURS)],
            "fuel_type": "PETROL" if roll < 0.6 else "DIESEL",
            "owner_name_masked": None,
            "registration_date": f"{2005 + int(roll * 19)}-0{1 + int(roll * 8) % 9}-15",
            "insurance_valid": roll < 0.9,
            "fitness_valid": roll < 0.93,
            "blacklisted": blacklisted,
            "blacklist_reason": "NON_PAYMENT_OF_TAX" if blacklisted else None,
        }


def _mask_name(name: object) -> str | None:
    """Reduce an owner name to initials + last-name tail for console display."""
    if not isinstance(name, str) or not name.strip():
        return None
    parts = name.strip().split()
    if len(parts) == 1:
        return f"{parts[0][0]}{'*' * max(len(parts[0]) - 1, 1)}"
    return " ".join(p[0] + "*" * max(len(p) - 1, 1) for p in parts[:-1]) + f" {parts[-1]}"


# ---------------------------------------------------------------------------
# Priority 0 alert dispatch
# ---------------------------------------------------------------------------

_SEVERITY_BY_CLASSIFICATION: Final = {
    MatchClassification.WANTED_CRIMINAL.value: "P0",
    MatchClassification.STOLEN_VEHICLE.value: "P0",
    MatchClassification.NONE.value: "P2",
}


@dataclass(slots=True)
class ThreatAlert:
    """The wire shape consumed by the TRINETRA central-command dashboard."""

    alert_id: str
    priority: str
    classification: str
    plate_number: str | None
    camera_id: str | None
    latitude: float | None
    longitude: float | None
    detected_at: int
    dispatched_at: int
    confidence: float
    vehicle: dict[str, Any] = field(default_factory=dict)
    subject: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "priority": self.priority,
            "classification": self.classification,
            "plate_number": self.plate_number,
            "camera_id": self.camera_id,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "detected_at": self.detected_at,
            "dispatched_at": self.dispatched_at,
            "confidence": self.confidence,
            "vehicle": self.vehicle,
            "subject": self.subject,
            "evidence": self.evidence,
            "schema_version": 1,
        }


class AlertDispatcher:
    """Turns adapter hits into P0 alerts and streams them to central command.

    A single background task owns the WebSocket so the detection path only ever
    touches a bounded in-memory queue. If the socket is down the queue drops the
    oldest alert rather than growing without limit.
    """

    def __init__(
        self,
        ws_url: str | None = None,
        *,
        api_key: str | None = None,
        queue_maxsize: int = 1000,
        reconnect_max_delay: float = 30.0,
    ) -> None:
        self._ws_url = ws_url or os.getenv("P0_ALERT_WS_URL", "ws://central-command/alerts/p0")
        self._api_key = api_key if api_key is not None else os.getenv("P0_ALERT_API_KEY")
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_maxsize)
        self._reconnect_max_delay = reconnect_max_delay
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._connected = asyncio.Event()
        self.dropped_alerts = 0

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._running = True
            self._task = asyncio.create_task(self._pump(), name="p0-alert-dispatcher")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._connected.clear()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # -- ingestion ---------------------------------------------------------

    def build_alert(
        self,
        *,
        cctns_result: dict[str, Any],
        vahan_result: dict[str, Any] | None = None,
        camera_id: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        detected_at: int | None = None,
        track_id: str | None = None,
        snapshot_uri: str | None = None,
    ) -> ThreatAlert | None:
        """Construct a P0 payload from adapter results, or ``None`` on no-hit.

        A VAHAN blacklist flag alone is enough to raise the alert even when
        CCTNS reports no warrant.
        """
        vahan = vahan_result or {}
        classification = str(cctns_result.get("classification", MatchClassification.NONE.value))
        blacklisted = bool(vahan.get("blacklisted"))
        if classification == MatchClassification.NONE.value and not blacklisted:
            return None
        if classification == MatchClassification.NONE.value:
            classification = MatchClassification.STOLEN_VEHICLE.value

        plate = cctns_result.get("plate_number") or vahan.get("plate_number")
        detected = detected_at or cctns_result.get("checked_at") or now_epoch_ms()
        seed = f"{plate}|{camera_id}|{detected}|{classification}"
        return ThreatAlert(
            alert_id=hashlib.sha256(seed.encode()).hexdigest()[:32],
            priority=_SEVERITY_BY_CLASSIFICATION.get(classification, "P1"),
            classification=classification,
            plate_number=plate,
            camera_id=camera_id,
            latitude=latitude,
            longitude=longitude,
            detected_at=int(detected),
            dispatched_at=now_epoch_ms(),
            confidence=float(cctns_result.get("confidence", 0.0) or 0.0),
            vehicle={
                "make": vahan.get("make"),
                "model": vahan.get("model"),
                "colour": vahan.get("colour"),
                "registration_status": vahan.get("registration_status"),
                "blacklisted": blacklisted,
                "blacklist_reason": vahan.get("blacklist_reason"),
            },
            subject={
                "name": cctns_result.get("subject_name"),
                "case_references": cctns_result.get("case_references", []),
            },
            evidence={
                "track_id": track_id,
                "snapshot_uri": snapshot_uri,
                "sources": [s for s in (cctns_result.get("source"), vahan.get("source")) if s],
            },
        )

    async def dispatch(
        self,
        *,
        cctns_result: dict[str, Any],
        vahan_result: dict[str, Any] | None = None,
        **context: Any,
    ) -> ThreatAlert | None:
        """Build and enqueue an alert. Returns the alert, or ``None`` on no-hit."""
        alert = self.build_alert(cctns_result=cctns_result, vahan_result=vahan_result, **context)
        if alert is None:
            return None
        await self.emit(alert)
        return alert

    async def emit(self, alert: ThreatAlert) -> None:
        """Enqueue a pre-built alert, evicting the oldest entry when saturated."""
        await self.start()
        payload = json.dumps(alert.to_dict(), separators=(",", ":"))
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                self._queue.task_done()
            self.dropped_alerts += 1
            logger.error(
                "P0 alert queue saturated; dropped oldest alert (total dropped=%d)",
                self.dropped_alerts,
            )
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(payload)

    # -- transport ---------------------------------------------------------

    async def _pump(self) -> None:
        delay = 0.5
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else None
        while self._running:
            try:
                async with websockets.connect(
                    self._ws_url,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=10,
                    max_queue=64,
                ) as socket:
                    self._connected.set()
                    delay = 0.5
                    logger.info("P0 alert channel connected: %s", self._ws_url)
                    while self._running:
                        payload = await self._queue.get()
                        try:
                            await socket.send(payload)
                        except Exception:
                            # Re-queue at the head-equivalent position so the
                            # alert survives the reconnect.
                            with contextlib.suppress(asyncio.QueueFull):
                                self._queue.put_nowait(payload)
                            raise
                        finally:
                            self._queue.task_done()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._connected.clear()
                logger.warning("P0 alert channel down (%s); retrying in %.1fs", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._reconnect_max_delay) * (0.75 + random.random() * 0.5)
        self._connected.clear()


# ---------------------------------------------------------------------------
# Convenience façade
# ---------------------------------------------------------------------------


async def screen_target(
    plate_number: str,
    person_attributes: dict[str, Any] | None = None,
    *,
    egujcop: eGujCopAdapter,
    vahan: VahanAdapter,
    dispatcher: AlertDispatcher | None = None,
    **context: Any,
) -> dict[str, Any]:
    """Run both registry lookups concurrently and dispatch any resulting alert."""
    cctns_result, vahan_result = await asyncio.gather(
        egujcop.check_warrants_and_stolen(plate_number, person_attributes),
        vahan.verify_vehicle_registration(plate_number),
    )
    alert = None
    if dispatcher is not None:
        alert = await dispatcher.dispatch(
            cctns_result=cctns_result, vahan_result=vahan_result, **context
        )
    return {
        "cctns": cctns_result,
        "vahan": vahan_result,
        "alert": alert.to_dict() if alert else None,
    }
