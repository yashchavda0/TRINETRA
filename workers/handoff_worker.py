"""Predictive handoff worker: the driver that ties the pipeline together.

Run with:
    python -m workers.handoff_worker

What it does, per message on ``surveillance-events-raw``:

1. Decodes the protobuf ``SurveillanceEvent`` produced by cmd/ingestion_gateway
   or by the ANPR reader, and rejects one whose identifiers are unusable rather
   than letting it fail inside the INSERT and vanish.
2. Persists the detection idempotently on ``event_id``, including the plate.
3. When - and only when - a valid 512-float ``feature_embedding`` is present,
   resolves the detection to a Re-ID identity through
   :class:`engine.vector_matcher.VectorMatcher`, creating a new target when
   nothing matches above threshold, and updates the wake-up state machine.
4. Whatever the embedding, matches any plate against the watchlist and screens
   it against eGujCop/VAHAN, publishing the resulting alert to the API's
   fan-out endpoint.

Point 3 and point 4 are deliberately independent. Gating plate handling on a
Re-ID identity made every plate read from an embedding-less producer invisible:
stored, never screened, never alerted.

Note on naming: this is the *predictive handoff engine*, which the tender counts
as one component of its Model 4 (Central VMS). It is not Model 4 itself.

A scheduler task runs alongside the consumer and supplies the two callers the
engine has always been missing: ``CameraStateMachine.tick()`` (so cooldowns and
stale pre-activations actually expire) and ``ActiveTargetsCache.purge_expired()``.
It is also where handoff prediction happens, because the prediction's temporal
term is a function of time elapsed since the target was last seen — a single
prediction at detection time would always score ~0 for distant cameras.

The STGCN weights are random: no checkpoint exists in this repository. The
wiring below is real; the probabilities it produces are not yet meaningful.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import math
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any, Final

import asyncpg
import httpx
import torch

from adapters.external_db_bridge import (
    AlertDispatcher,
    ThreatAlert,
    VahanAdapter,
    build_alert_id,
    eGujCopAdapter,
    normalise_plate,
    now_epoch_ms,
    screen_target,
)
from app import database
from app.config import Settings, get_settings
from app.main import JsonLogFormatter
from engine.tracking_pipeline import (
    PredictiveHandoffManager,
    STGCNPredictor,
    SpatialCameraGraph,
)
from engine.vector_matcher import (
    EMBEDDING_DIM,
    ActiveTargetsCache,
    CameraState,
    CameraStateMachine,
    VectorMatcher,
)

try:
    from confluent_kafka import Consumer, KafkaError, KafkaException
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit(
        "confluent-kafka is not installed; run: pip install -r requirements.txt"
    ) from exc

try:
    from generated import surveillance_event_pb2 as pb
except ImportError as exc:  # pragma: no cover - codegen guard
    raise SystemExit(
        "generated/surveillance_event_pb2.py is missing. Generate it first:\n"
        "  .\\scripts\\gen_proto.ps1\n"
        "or: python -m grpc_tools.protoc -I proto --python_out=generated "
        "proto/surveillance_event.proto"
    ) from exc

logger: Final = logging.getLogger("workers.handoff")

EARTH_RADIUS_M: Final = 6_371_008.8

_INSERT_DETECTION_SQL: Final = """
    INSERT INTO detections (
        event_id, camera_id, department_id, department_code_tag, timestamp_utc_ms,
        object_class, bbox_x_min, bbox_y_min, bbox_x_max, bbox_y_max,
        attributes, track_id, target_id, detection_geom, azimuth_degrees,
        embedding_accepted, plate_number, plate_confidence, snapshot_uri
    )
    VALUES (
        $1, $2, $3, $4, $5,
        $6, $7, $8, $9, $10,
        $11::jsonb, $12, $13,
        CASE
            WHEN $14::double precision IS NULL OR $15::double precision IS NULL THEN NULL
            ELSE ST_SetSRID(ST_MakePoint($14, $15), 4326)
        END,
        $16, $17, $18, $19, $20
    )
    ON CONFLICT (event_id) DO NOTHING
    RETURNING event_id
"""

_UPSERT_EMBEDDING_SQL: Final = """
    INSERT INTO target_embeddings (
        target_id, embedding, last_seen_camera_id, last_seen_utc_ms
    )
    VALUES ($1, $2::real[], $3, $4)
    ON CONFLICT (target_id) DO UPDATE
    SET embedding           = EXCLUDED.embedding,
        last_seen_camera_id = EXCLUDED.last_seen_camera_id,
        last_seen_utc_ms    = EXCLUDED.last_seen_utc_ms,
        sighting_count      = target_embeddings.sighting_count + 1,
        updated_at          = clock_timestamp()
"""

_UPSERT_WAKE_STATE_SQL: Final = """
    INSERT INTO camera_wake_state (
        camera_id, state, target_id, handoff_probability, entered_at,
        cooldown_until, updated_at
    )
    VALUES (
        $1, $2, $3, $4, clock_timestamp(),
        CASE WHEN $5::double precision IS NULL
             THEN NULL
             ELSE clock_timestamp() + ($5 * INTERVAL '1 second')
        END,
        clock_timestamp()
    )
    ON CONFLICT (camera_id) DO UPDATE
    SET state               = EXCLUDED.state,
        target_id           = EXCLUDED.target_id,
        handoff_probability = EXCLUDED.handoff_probability,
        entered_at          = CASE
                                  WHEN camera_wake_state.state <> EXCLUDED.state
                                  THEN clock_timestamp()
                                  ELSE camera_wake_state.entered_at
                              END,
        cooldown_until      = EXCLUDED.cooldown_until,
        updated_at          = clock_timestamp()
"""

_LOAD_EMBEDDINGS_SQL: Final = """
    SELECT target_id, embedding, last_seen_camera_id, last_seen_utc_ms
    FROM target_embeddings
    WHERE last_seen_utc_ms IS NOT NULL
    ORDER BY last_seen_utc_ms DESC
    LIMIT $1
"""

# Both liveness conditions are applied here, at read time. `expires_at` has no
# sweeper anywhere in the system, so an expired entry stays in the table and
# must be filtered out on every load.
_LOAD_WATCHLIST_SQL: Final = """
    SELECT id::text, plate_number, target_id, classification, priority,
           reason, case_reference, department_id
    FROM watchlist
    WHERE is_active
      AND (expires_at IS NULL OR expires_at > clock_timestamp())
"""

# Ceiling on the screening-cooldown map before it is swept. High enough that a
# busy fleet never sweeps on the hot path, low enough to bound memory.
_SCREEN_CACHE_MAX: Final = 50_000


def haversine_meters(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Great-circle distance in metres between two WGS84 points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, a)))


def initial_bearing_degrees(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Compass bearing from point 1 to point 2: 0 = true north, clockwise."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_lambda = math.radians(lon2 - lon1)
    y = math.sin(d_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(
        d_lambda
    )
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


@dataclass
class TargetTrack:
    """Latest kinematic state of one tracked identity."""

    target_id: str
    camera_id: str
    latitude: float
    longitude: float
    timestamp_utc_ms: int
    velocity_mps: float = 0.0
    heading_degrees: float = 0.0
    plate_number: str | None = None


@dataclass
class DecodedEvent:
    """The subset of a SurveillanceEvent this worker acts on."""

    event_id: str
    camera_id: str
    department_id: str | None
    department_tag: int
    timestamp_utc_ms: int
    latitude: float | None
    longitude: float | None
    azimuth_degrees: float | None
    object_class: str
    bbox: tuple[float, float, float, float] | None
    attributes: dict[str, str]
    embedding: list[float]

    @property
    def track_id(self) -> str | None:
        return self.attributes.get("track_id")

    @property
    def plate_number(self) -> str | None:
        """The plate as read, normalised to the form the registry stores.

        Normalising here rather than at each use is what makes the stored
        `detections.plate_number`, the watchlist comparison and the search
        query all agree on one spelling. A producer that sends "GJ 01 AB 1234"
        must be findable by someone typing "GJ01AB1234".
        """
        raw = self.attributes.get("license_plate")
        if not raw:
            return None
        try:
            return normalise_plate(raw)
        except ValueError:
            # Not a plausible registration mark. Keep the raw text - it is
            # evidence of what the reader saw - but it will not match a
            # watchlist entry, which is the correct outcome for a misread.
            return raw.strip().upper()[:24] or None

    @property
    def plate_confidence(self) -> float | None:
        """ANPR confidence, when the producer supplied one."""
        raw = self.attributes.get("license_plate_conf")
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if 0.0 <= value <= 1.0 else None

    @property
    def snapshot_uri(self) -> str | None:
        return self.attributes.get("snapshot_uri")


def decode_event(raw: bytes) -> DecodedEvent:
    """Parse one protobuf message into the worker's own value object."""
    event = pb.SurveillanceEvent()
    event.ParseFromString(raw)

    has_location = event.HasField("location")
    has_bbox = event.HasField("bounding_box")

    department_name = pb.DepartmentCode.Name(event.department_code)
    return DecodedEvent(
        event_id=event.event_id,
        camera_id=event.camera_id,
        department_id=(
            None if department_name == "DEPARTMENT_CODE_UNSPECIFIED" else department_name
        ),
        department_tag=int(event.department_code),
        timestamp_utc_ms=int(event.timestamp_utc_ms),
        latitude=float(event.location.latitude) if has_location else None,
        longitude=float(event.location.longitude) if has_location else None,
        azimuth_degrees=(
            float(event.location.azimuth_degrees) if has_location else None
        ),
        object_class=pb.ObjectClass.Name(event.object_class),
        bbox=(
            (
                float(event.bounding_box.x_min),
                float(event.bounding_box.y_min),
                float(event.bounding_box.x_max),
                float(event.bounding_box.y_max),
            )
            if has_bbox
            else None
        ),
        attributes=dict(event.attributes),
        embedding=list(event.feature_embedding),
    )


class HandoffWorker:
    """Owns the consumer, the engine, the adapters and the scheduler."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.device = torch.device(
            settings.engine_device
            if settings.engine_device
            else ("cuda" if torch.cuda.is_available() else "cpu")
        )

        self.cache = ActiveTargetsCache(
            device=self.device, max_targets=settings.max_active_targets
        )
        self.matcher = VectorMatcher(
            self.cache,
            device=self.device,
            similarity_threshold=settings.similarity_threshold,
        )
        self.state_machine = CameraStateMachine(
            probability_threshold=settings.handoff_probability_threshold,
            cooldown_seconds=settings.cooldown_seconds,
            pre_activation_timeout_seconds=settings.pre_activation_timeout_seconds,
        )

        self.graph: SpatialCameraGraph | None = None
        self.handoff: PredictiveHandoffManager | None = None
        self._graph_lock = asyncio.Lock()

        self.egujcop = eGujCopAdapter(
            settings.egujcop_base_url, settings.egujcop_api_key
        )
        self.vahan = VahanAdapter(settings.vahan_base_url, settings.vahan_api_key)
        # Used for build_alert only. start() is never called: alerts travel over
        # HTTP to the API's fan-out endpoint, which also writes the audit row.
        # Letting the dispatcher own a second WebSocket would double-deliver.
        self.alert_builder = AlertDispatcher(settings.p0_alert_ws_url)

        self._tracks: dict[str, TargetTrack] = {}
        self._consumer: Consumer | None = None
        self._publisher: httpx.AsyncClient | None = None
        self._stop = asyncio.Event()

        # Watchlist, indexed twice because a plate read and a Re-ID identity are
        # two different match paths. Swapped wholesale under the lock, never
        # mutated in place, so a reader always sees one consistent generation.
        self._watchlist_plates: dict[str, dict[str, Any]] = {}
        self._watchlist_targets: dict[str, dict[str, Any]] = {}
        self._watchlist_lock = asyncio.Lock()

        # (plate, camera_id) -> monotonic time of the last external screening.
        self._screened: dict[tuple[str, str], float] = {}

        self.processed = 0
        self.rejected_embeddings = 0
        self.rejected_identifiers = 0
        self.alerts_published = 0
        self.watchlist_hits = 0

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        await database.connect(self.settings)
        await self._reload_graph()
        await self._rehydrate_targets()
        # Loaded here rather than left to the scheduler: that loop sleeps at the
        # top, so anything not loaded now is cold for a full interval - and a
        # cold watchlist means a wanted vehicle passes unremarked.
        await self._reload_watchlist()

        self._publisher = httpx.AsyncClient(
            timeout=httpx.Timeout(5.0),
            headers=(
                {"X-API-Key": self.settings.p0_alert_api_key}
                if self.settings.p0_alert_api_key
                else {}
            ),
        )

        self._consumer = Consumer(
            {
                "bootstrap.servers": self.settings.kafka_bootstrap_servers,
                "group.id": self.settings.kafka_consumer_group,
                "auto.offset.reset": "earliest",
                # Offsets are committed only after the detection is persisted, so
                # a crash replays rather than loses evidence.
                "enable.auto.commit": False,
                "socket.keepalive.enable": True,
            }
        )
        self._consumer.subscribe([self.settings.kafka_topic_raw])

        logger.info(
            "worker started",
            extra={
                "device": str(self.device),
                "topic": self.settings.kafka_topic_raw,
                "bootstrap": self.settings.kafka_bootstrap_servers,
                "group": self.settings.kafka_consumer_group,
                "graph_nodes": self.graph.num_nodes if self.graph else 0,
                "graph_edges": self.graph.num_edges if self.graph else 0,
                "watchlist_plates": len(self._watchlist_plates),
                "watchlist_targets": len(self._watchlist_targets),
            },
        )

    async def stop(self) -> None:
        self._stop.set()

        if self._consumer is not None:
            consumer, self._consumer = self._consumer, None
            await asyncio.to_thread(consumer.close)

        if self._publisher is not None:
            publisher, self._publisher = self._publisher, None
            await publisher.aclose()

        await self.egujcop.aclose()
        await self.vahan.aclose()
        await database.disconnect()

        logger.info(
            "worker stopped",
            extra={
                "processed": self.processed,
                "rejected_embeddings": self.rejected_embeddings,
                "rejected_identifiers": self.rejected_identifiers,
                "watchlist_hits": self.watchlist_hits,
                "alerts_published": self.alerts_published,
            },
        )

    # -- graph and cache bootstrap -----------------------------------------

    async def _reload_graph(self) -> None:
        """(Re)build the camera graph and the handoff manager from the registry."""
        graph = SpatialCameraGraph(
            self.settings.database_url,
            distance_threshold_m=self.settings.edge_distance_threshold_m,
            device=self.device,
        )
        try:
            await graph.load()
        except RuntimeError:
            logger.exception("camera graph load failed; keeping previous graph")
            return

        if graph.num_nodes == 0:
            logger.warning(
                "camera graph is empty; register ACTIVE cameras before expecting handoffs"
            )
            async with self._graph_lock:
                self.graph = graph
                self.handoff = None
            return

        manager = PredictiveHandoffManager(
            graph,
            STGCNPredictor(),
            probability_threshold=self.settings.handoff_probability_threshold,
            device=self.device,
        )
        # Untrained weights: this only allocates embeddings, it does not make the
        # priors meaningful.
        manager.refresh_embeddings()

        async with self._graph_lock:
            self.graph = graph
            self.handoff = manager

        logger.info(
            "camera graph refreshed",
            extra={"nodes": graph.num_nodes, "edges": graph.num_edges},
        )

    async def _rehydrate_targets(self) -> None:
        """Reload persisted Re-ID vectors so a restart does not lose identities."""
        pool = database.get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                _LOAD_EMBEDDINGS_SQL, self.settings.max_active_targets
            )

        restored = 0
        for row in rows:
            embedding = list(row["embedding"] or [])
            if len(embedding) != EMBEDDING_DIM:
                continue
            vector = torch.tensor(embedding, dtype=torch.float32).unsqueeze(0)
            self.cache.upsert(row["target_id"], vector)
            self._tracks[row["target_id"]] = TargetTrack(
                target_id=row["target_id"],
                camera_id=str(row["last_seen_camera_id"]),
                latitude=0.0,
                longitude=0.0,
                timestamp_utc_ms=int(row["last_seen_utc_ms"] or 0),
            )
            restored += 1

        if restored:
            logger.info("rehydrated %d target embedding(s) from PostgreSQL", restored)

    async def _reload_watchlist(self) -> None:
        """Rebuild the watchlist indexes from the database.

        Built off to the side and swapped under the lock, the same shape as
        _reload_graph: a failed refresh must leave the previous watchlist in
        place rather than empty it, because an empty watchlist silently stops
        matching instead of failing loudly.
        """
        try:
            pool = database.get_pool()
            async with pool.acquire() as connection:
                rows = await connection.fetch(_LOAD_WATCHLIST_SQL)
        except (asyncpg.PostgresError, RuntimeError):
            logger.exception("watchlist reload failed; keeping the previous copy")
            return

        by_plate: dict[str, dict[str, Any]] = {}
        by_target: dict[str, dict[str, Any]] = {}

        for row in rows:
            entry = dict(row)
            if entry.get("plate_number"):
                # Normalise on load so a hand-typed entry matches a reader's
                # output. The unique partial index guarantees one active row per
                # plate string, but not that the string is well formed.
                try:
                    by_plate[normalise_plate(entry["plate_number"])] = entry
                except ValueError:
                    logger.warning(
                        "watchlist entry has an unusable plate; skipping",
                        extra={"watchlist_id": entry["id"], "plate": entry["plate_number"]},
                    )
            if entry.get("target_id"):
                by_target[entry["target_id"]] = entry

        async with self._watchlist_lock:
            self._watchlist_plates = by_plate
            self._watchlist_targets = by_target

        logger.info(
            "watchlist refreshed",
            extra={"plates": len(by_plate), "targets": len(by_target)},
        )

    # -- consumer loop -----------------------------------------------------

    async def consume_forever(self) -> None:
        assert self._consumer is not None, "start() must run first"

        while not self._stop.is_set():
            message = await asyncio.to_thread(
                self._consumer.poll, self.settings.kafka_poll_timeout_seconds
            )
            if message is None:
                continue

            error = message.error()
            if error is not None:
                if error.code() == KafkaError._PARTITION_EOF:
                    continue
                logger.error("kafka error", extra={"error": str(error)})
                continue

            try:
                await self._handle_message(message.value())
            except Exception:
                # A single malformed or unpersistable event must not kill the
                # consumer; the offset is still committed so it cannot wedge the
                # partition. The payload is on the bus if it needs replaying.
                logger.exception("event processing failed")

            # Keyword arguments are load-bearing: Consumer.commit's signature is
            # (message=None, offsets=None, asynchronous=True), so commit(msg,
            # False) binds False to `offsets` and raises TypeError - which is
            # not a KafkaException, escapes any suppression, and kills this
            # loop while the rest of the worker keeps running.
            try:
                await asyncio.to_thread(
                    functools.partial(
                        self._consumer.commit, message=message, asynchronous=False
                    )
                )
            except KafkaException as exc:
                # A failed commit means this message may be redelivered. The
                # detection insert is idempotent on event_id, so a replay is
                # harmless - but it must be visible rather than silent.
                logger.warning("offset commit failed", extra={"error": str(exc)})

    async def _handle_message(self, raw: bytes | None) -> None:
        if not raw:
            return

        event = decode_event(raw)

        # An event whose identifiers are unusable cannot be stored: event_id is
        # the primary key and camera_id is NOT NULL, so a malformed one raises
        # inside the INSERT, the offset is committed anyway, and the event
        # disappears without trace. Reject it here, where it can be counted.
        if _as_uuid(event.event_id) is None or _as_uuid(event.camera_id) is None:
            self.rejected_identifiers += 1
            logger.warning(
                "discarding event with unusable identifiers",
                extra={
                    "event_id": event.event_id,
                    "camera_id": event.camera_id,
                },
            )
            return

        # A 512-float embedding is required for re-identification and for
        # nothing else. An ANPR reader publishes plates without one, and such an
        # event is still a real observation: it is persisted, and its plate is
        # still screened. Only the Re-ID and handoff paths are skipped.
        embedding_ok = len(event.embedding) == EMBEDDING_DIM
        if not embedding_ok and event.embedding:
            # Empty is the normal, expected shape from a plate-only producer.
            # A wrong *length* is a contract violation and worth a warning.
            self.rejected_embeddings += 1
            logger.warning(
                "rejecting feature_embedding of wrong length",
                extra={
                    "event_id": event.event_id,
                    "camera_id": event.camera_id,
                    "embedding_length": len(event.embedding),
                    "expected": EMBEDDING_DIM,
                },
            )

        target_id: str | None = None
        similarity: float | None = None
        if embedding_ok:
            target_id, similarity = await self._assign_target(event)

        await self._persist_detection(event, target_id, embedding_ok)
        self.processed += 1

        # Fire-and-forget to the live console feed. Persistence above is the
        # durable fact; this is only the notification, so a dead API here must
        # never fail the message that already committed.
        await self._publish_detection(event, target_id)

        # Re-identification state only means something when there is an identity.
        if target_id is not None:
            await self._advance_states(event, target_id)

        # Plate handling is deliberately outside that guard. Gating it on
        # target_id made every plate read from an embedding-less producer
        # invisible: stored, never screened, never alerted.
        if event.plate_number:
            await self._handle_plate(event, target_id)

        logger.info(
            "detection processed",
            extra={
                "event_id": event.event_id,
                "camera_id": event.camera_id,
                "target_id": target_id,
                "plate_number": event.plate_number,
                "similarity": round(similarity, 4) if similarity is not None else None,
                "object_class": event.object_class,
            },
        )

    # -- re-identification -------------------------------------------------

    async def _assign_target(self, event: DecodedEvent) -> tuple[str, float | None]:
        """Match the detection to a known identity, or mint a new one."""
        vector = torch.tensor(event.embedding, dtype=torch.float32).unsqueeze(0)

        best_id: str | None = None
        best_score = -1.0
        for candidate_id in self.cache.target_ids():
            try:
                result = self.matcher.best_match(vector, candidate_id)
            except KeyError:
                # Purged by the scheduler between listing and scoring.
                continue
            if result is None:
                continue
            _, score = result
            if score > best_score:
                best_id, best_score = candidate_id, score

        if best_id is None:
            best_id = uuid.uuid4().hex
            best_score = 1.0
            logger.info(
                "new target identity created",
                extra={"target_id": best_id, "camera_id": event.camera_id},
            )

        self.cache.upsert(best_id, vector)
        self._update_track(event, best_id)

        pool = database.get_pool()
        async with pool.acquire() as connection:
            await connection.execute(
                _UPSERT_EMBEDDING_SQL,
                best_id,
                [float(value) for value in event.embedding],
                _as_uuid(event.camera_id),
                event.timestamp_utc_ms,
            )

        return best_id, (best_score if best_score >= 0 else None)

    def _update_track(self, event: DecodedEvent, target_id: str) -> None:
        """Recompute the target's speed and heading from consecutive sightings."""
        previous = self._tracks.get(target_id)
        velocity = 0.0
        heading = 0.0

        if (
            previous is not None
            and event.latitude is not None
            and event.longitude is not None
            and previous.latitude
            and previous.longitude
            and event.timestamp_utc_ms > previous.timestamp_utc_ms
        ):
            elapsed = (event.timestamp_utc_ms - previous.timestamp_utc_ms) / 1000.0
            distance = haversine_meters(
                previous.latitude, previous.longitude, event.latitude, event.longitude
            )
            if elapsed > 0:
                velocity = distance / elapsed
            if distance > 1.0:
                heading = initial_bearing_degrees(
                    previous.latitude,
                    previous.longitude,
                    event.latitude,
                    event.longitude,
                )
            else:
                # Same spot: keep the last known heading rather than snapping to 0.
                heading = previous.heading_degrees

        self._tracks[target_id] = TargetTrack(
            target_id=target_id,
            camera_id=event.camera_id,
            latitude=event.latitude if event.latitude is not None else 0.0,
            longitude=event.longitude if event.longitude is not None else 0.0,
            timestamp_utc_ms=event.timestamp_utc_ms,
            velocity_mps=velocity,
            heading_degrees=heading,
            plate_number=event.plate_number or (previous.plate_number if previous else None),
        )

    # -- wake-up state machine --------------------------------------------

    async def _advance_states(self, event: DecodedEvent, target_id: str) -> None:
        """Confirm the predicted handoff and release the camera left behind."""
        touched: set[str] = set()

        if self.state_machine.state_of(event.camera_id) is CameraState.PRE_ACTIVATED:
            self.state_machine.confirm_match(event.camera_id, target_id)
            touched.add(event.camera_id)

        for camera_id in self.state_machine.cameras_in_state(
            CameraState.ACTIVE_TRACKING
        ):
            record = self.state_machine.snapshot().get(camera_id)
            if (
                camera_id != event.camera_id
                and record is not None
                and record.target_id == target_id
            ):
                self.state_machine.target_exited_fov(camera_id)
                touched.add(camera_id)

        await self._persist_states(touched)

    async def _persist_states(self, camera_ids: set[str]) -> None:
        if not camera_ids:
            return

        snapshot = self.state_machine.snapshot()
        pool = database.get_pool()
        async with pool.acquire() as connection:
            for camera_id in camera_ids:
                record = snapshot.get(camera_id)
                if record is None:
                    continue
                camera_uuid = _as_uuid(camera_id)
                if camera_uuid is None:
                    continue
                remaining_cooldown: float | None = None
                if record.cooldown_until is not None:
                    remaining_cooldown = max(
                        0.0, record.cooldown_until - time.monotonic()
                    )
                try:
                    await connection.execute(
                        _UPSERT_WAKE_STATE_SQL,
                        camera_uuid,
                        record.state.value,
                        record.target_id,
                        float(record.handoff_probability),
                        remaining_cooldown,
                    )
                except asyncpg.ForeignKeyViolationError:
                    # The state machine can hold a camera that has since been
                    # deleted from the registry; drop the mirror, keep the memory.
                    logger.warning(
                        "wake state not mirrored: camera absent from registry",
                        extra={"camera_id": camera_id},
                    )

    # -- persistence -------------------------------------------------------

    async def _persist_detection(
        self, event: DecodedEvent, target_id: str | None, embedding_ok: bool
    ) -> None:
        bbox = event.bbox or (None, None, None, None)
        pool = database.get_pool()
        async with pool.acquire() as connection:
            await connection.execute(
                _INSERT_DETECTION_SQL,
                _as_uuid(event.event_id),
                _as_uuid(event.camera_id),
                event.department_id,
                event.department_tag,
                event.timestamp_utc_ms,
                event.object_class,
                bbox[0],
                bbox[1],
                bbox[2],
                bbox[3],
                json.dumps(event.attributes),
                event.track_id,
                target_id,
                event.longitude,  # $14 -> ST_MakePoint X
                event.latitude,  # $15 -> ST_MakePoint Y
                event.azimuth_degrees,
                embedding_ok,
                event.plate_number,
                event.plate_confidence,
                event.snapshot_uri,
            )

    async def _publish_detection(self, event: DecodedEvent, target_id: str | None) -> None:
        """Notify the API's live feed. Best-effort: never raises past this call."""
        if self._publisher is None:
            return

        frame: dict[str, Any] = {
            "event_id": event.event_id,
            "camera_id": event.camera_id,
            "department_id": event.department_id,
            "timestamp_utc_ms": event.timestamp_utc_ms,
            "object_class": event.object_class,
            "plate_number": event.plate_number,
            "plate_confidence": event.plate_confidence,
            "track_id": event.track_id,
            "target_id": target_id,
            "latitude": event.latitude,
            "longitude": event.longitude,
            "snapshot_uri": event.snapshot_uri,
        }
        try:
            response = await self._publisher.post(
                self.settings.detection_publish_url, json=frame
            )
            if response.status_code >= 400:
                logger.warning(
                    "detection publish rejected",
                    extra={"event_id": event.event_id, "status": response.status_code},
                )
        except httpx.HTTPError as exc:
            logger.error(
                "detection publish failed",
                extra={"event_id": event.event_id, "error": str(exc)},
            )

    # -- plate handling ----------------------------------------------------

    async def _handle_plate(self, event: DecodedEvent, target_id: str | None) -> None:
        """Match a plate against the watchlist, then screen it externally.

        Runs whether or not the event carried a Re-ID embedding: a plate read is
        an observation in its own right, and an ANPR reader publishes nothing
        else.

        Screening is rate-limited per (plate, camera). A vehicle sitting in view
        produces a plate on every sampled frame, and each unthrottled screening
        costs two outbound calls to the state registries.
        """
        plate = event.plate_number
        if not plate:
            return

        # The watchlist is local and authoritative for "we are looking for this
        # one", so it is checked first and is never rate-limited - a hit must
        # not be suppressed because the same vehicle was screened moments ago.
        await self._match_watchlist(event, plate, target_id)

        if not self._should_screen(plate, event.camera_id):
            return

        await self._screen_plate(event, plate, target_id)

    def _should_screen(self, plate: str, camera_id: str) -> bool:
        """True when this (plate, camera) pair is outside its cooldown."""
        key = (plate, camera_id)
        now = time.monotonic()
        last = self._screened.get(key)
        if last is not None and now - last < self.settings.plate_screen_ttl_seconds:
            return False

        self._screened[key] = now
        # Opportunistic eviction: the map is only ever read by this method, so a
        # sweep here is cheaper than a background task and cannot leak.
        if len(self._screened) > _SCREEN_CACHE_MAX:
            cutoff = now - self.settings.plate_screen_ttl_seconds
            self._screened = {
                pair: seen for pair, seen in self._screened.items() if seen >= cutoff
            }
        return True

    async def _match_watchlist(
        self, event: DecodedEvent, plate: str, target_id: str | None
    ) -> None:
        """Raise an alert when this plate or identity is on the watchlist."""
        entry = self._watchlist_plates.get(plate)
        if entry is None and target_id is not None:
            entry = self._watchlist_targets.get(target_id)
        if entry is None:
            return

        # build_alert only accepts adapter-shaped results and derives priority
        # from a three-key map, while a watchlist entry carries its own
        # classification and priority. The ThreatAlert is therefore constructed
        # directly - but with the same alert_id seed, so ON CONFLICT (alert_id)
        # still collapses a repeat sighting into one row.
        classification = entry["classification"]
        alert = ThreatAlert(
            alert_id=build_alert_id(
                plate, event.camera_id, event.timestamp_utc_ms, classification
            ),
            priority=entry["priority"],
            classification=classification,
            plate_number=plate,
            camera_id=event.camera_id,
            latitude=event.latitude,
            longitude=event.longitude,
            detected_at=event.timestamp_utc_ms,
            dispatched_at=now_epoch_ms(),
            confidence=event.plate_confidence or 1.0,
            vehicle={},
            subject={
                "reason": entry["reason"],
                "case_references": [entry["case_reference"]] if entry["case_reference"] else [],
            },
            evidence={
                "source": "WATCHLIST",
                "watchlist_id": entry["id"],
                "track_id": event.track_id or target_id,
                "snapshot_uri": event.snapshot_uri,
            },
        )

        self.watchlist_hits += 1
        logger.warning(
            "watchlist hit",
            extra={
                "plate": plate,
                "camera_id": event.camera_id,
                "classification": classification,
                "priority": entry["priority"],
            },
        )
        await self._publish_alert(alert.to_dict())

    async def _screen_plate(
        self, event: DecodedEvent, plate: str, target_id: str | None
    ) -> None:
        """Run the registry lookups and publish any resulting threat alert."""
        try:
            result = await screen_target(
                plate,
                {
                    key: value
                    for key, value in event.attributes.items()
                    if key not in {"license_plate", "track_id"}
                },
                egujcop=self.egujcop,
                vahan=self.vahan,
                dispatcher=None,
            )
        except Exception:
            logger.exception("registry screening failed", extra={"plate": plate})
            return

        alert = self.alert_builder.build_alert(
            cctns_result=result["cctns"],
            vahan_result=result["vahan"],
            camera_id=event.camera_id,
            latitude=event.latitude,
            longitude=event.longitude,
            detected_at=event.timestamp_utc_ms,
            track_id=event.track_id or target_id,
            snapshot_uri=event.snapshot_uri,
        )
        if alert is None:
            return

        await self._publish_alert(alert.to_dict())

    async def _publish_alert(self, frame: dict[str, Any]) -> None:
        if self._publisher is None:
            return

        try:
            response = await self._publisher.post(
                self.settings.alert_publish_url, json=frame
            )
        except httpx.HTTPError as exc:
            logger.error(
                "alert publish failed",
                extra={"alert_id": frame.get("alert_id"), "error": str(exc)},
            )
            return

        if response.status_code >= 400:
            logger.error(
                "alert publish rejected",
                extra={
                    "alert_id": frame.get("alert_id"),
                    "status_code": response.status_code,
                },
            )
            return

        self.alerts_published += 1
        logger.info(
            "alert published",
            extra={
                "alert_id": frame.get("alert_id"),
                "priority": frame.get("priority"),
                "classification": frame.get("classification"),
            },
        )

    # -- scheduler ---------------------------------------------------------

    async def scheduler_forever(self) -> None:
        """Expire state, purge stale targets and run handoff prediction."""
        last_graph_reload = time.monotonic()
        last_watchlist_reload = time.monotonic()

        while not self._stop.is_set():
            await asyncio.sleep(self.settings.scheduler_interval_seconds)

            try:
                reset = self.state_machine.tick()
                if reset:
                    await self._persist_states(set(reset))

                purged = self.cache.purge_expired(self.settings.target_ttl_seconds)
                for target_id in purged:
                    self._tracks.pop(target_id, None)

                await self._predict_handoffs()

                if (
                    time.monotonic() - last_graph_reload
                    >= self.settings.graph_reload_seconds
                ):
                    await self._reload_graph()
                    last_graph_reload = time.monotonic()

                # Refreshed far more often than the graph: adding a stolen
                # vehicle to the watchlist should take effect in seconds, not
                # after the five-minute graph cycle.
                if (
                    time.monotonic() - last_watchlist_reload
                    >= self.settings.watchlist_reload_seconds
                ):
                    await self._reload_watchlist()
                    last_watchlist_reload = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduler iteration failed")

    async def _predict_handoffs(self) -> None:
        """Score downstream cameras for every moving target and wake them."""
        async with self._graph_lock:
            manager = self.handoff
            graph = self.graph

        if manager is None or graph is None or graph.num_nodes == 0:
            return

        now_ms = int(time.time() * 1000)
        touched: set[str] = set()

        for track in list(self._tracks.values()):
            if track.velocity_mps <= 0.0 or track.camera_id not in graph.node_index:
                continue

            elapsed = max(0.0, (now_ms - track.timestamp_utc_ms) / 1000.0)
            try:
                predictions = manager.predict_next_cameras(
                    track.camera_id,
                    track.velocity_mps,
                    track.heading_degrees,
                    elapsed,
                )
            except KeyError:
                continue

            for camera_id, probability in predictions:
                new_state = self.state_machine.signal_from_stgcn(
                    camera_id, track.target_id, probability
                )
                if new_state is CameraState.PRE_ACTIVATED:
                    touched.add(camera_id)

        await self._persist_states(touched)


def _as_uuid(value: str | None) -> uuid.UUID | None:
    """Parse a UUID string, returning None when the producer sent something else."""
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        logger.warning("not a UUID; storing NULL", extra={"value": value})
        return None


def configure_logging(log_level: str) -> None:
    """Emit one JSON object per line, matching the API's log format."""
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)


async def run() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)

    worker = HandoffWorker(settings)
    await worker.start()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):
            # Windows ProactorEventLoop supports neither for all signals; the
            # KeyboardInterrupt path below covers it.
            loop.add_signal_handler(sig, worker._stop.set)

    consumer_task = asyncio.create_task(worker.consume_forever(), name="kafka-consumer")
    scheduler_task = asyncio.create_task(worker.scheduler_forever(), name="scheduler")

    # Neither task may die quietly. Without this, an exception escaping the
    # consumer loop leaves a process that looks healthy - the scheduler still
    # logging, the port still open - while nothing is being consumed at all.
    def _task_died(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.critical(
                "worker task exited unexpectedly; shutting down",
                extra={"task": task.get_name()},
                exc_info=exc,
            )
        else:
            logger.error("worker task exited early", extra={"task": task.get_name()})
        worker._stop.set()

    consumer_task.add_done_callback(_task_died)
    scheduler_task.add_done_callback(_task_died)

    try:
        await worker._stop.wait()
    except KeyboardInterrupt:
        worker._stop.set()
    finally:
        for task in (consumer_task, scheduler_task):
            task.cancel()
        for task in (consumer_task, scheduler_task):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await worker.stop()

    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
