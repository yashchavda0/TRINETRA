"""Scene event worker: persists services/vlm_agent's Tier B findings and fans
out the operationally urgent ones as threat alerts.

Run with:
    python -m workers.scene_event_worker

Deliberately a separate consumer/process from workers/handoff_worker.py, on
its own topic (``scene-events-raw``) and its own consumer group: a SceneEvent
is not a per-object detection, has nothing to do with Re-ID/handoff/watchlist
screening, and its volume and schema must never be able to affect that
pipeline. Every finding is persisted to ``scene_events`` for investigation
regardless of severity; only findings at or above
``vlm_agent_alert_confidence_threshold`` for an operationally urgent type
additionally reach ``threat_alerts`` through the same fan-out endpoint
workers/handoff_worker.py already uses, so there is exactly one alert UI.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import signal
import sys
import uuid
from dataclasses import dataclass
from typing import Any, Final

import httpx

from adapters.external_db_bridge import ThreatAlert, build_alert_id, now_epoch_ms
from app import database
from app.config import Settings, get_settings
from app.main import JsonLogFormatter

try:
    from confluent_kafka import Consumer, KafkaError, KafkaException
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit(
        "confluent-kafka is not installed; run: pip install -r requirements.txt"
    ) from exc

try:
    from generated import scene_event_pb2 as scene_pb
except ImportError as exc:  # pragma: no cover - codegen guard
    raise SystemExit(
        "generated/scene_event_pb2.py is missing. Run scripts\\gen_proto.ps1"
    ) from exc

logger: Final = logging.getLogger("workers.scene_event")

# Event types urgent enough to also raise a threat alert, given sufficient
# confidence. LOITERING and UNUSUAL_ACTIVITY are investigative by nature -
# they surface through GET /api/v1/scene-events, not the P0 alert channel.
_ALERTABLE_EVENT_TYPES: Final = frozenset({"COLLISION", "WRONG_WAY", "CROWD_DENSITY"})

_INSERT_SCENE_EVENT_SQL: Final = """
    INSERT INTO scene_events (
        scene_event_id, camera_id, window_start_utc_ms, window_end_utc_ms,
        event_type, confidence, rationale, implicated_target_ids, clip_uri,
        model_version, scene_event_geom
    )
    VALUES (
        $1, $2, $3, $4,
        $5, $6, $7, $8, $9,
        $10,
        CASE
            WHEN $11::double precision IS NULL OR $12::double precision IS NULL THEN NULL
            ELSE ST_SetSRID(ST_MakePoint($11, $12), 4326)
        END
    )
    ON CONFLICT (scene_event_id) DO NOTHING
    RETURNING scene_event_id
"""


@dataclass
class DecodedSceneEvent:
    scene_event_id: str
    camera_id: str
    window_start_utc_ms: int
    window_end_utc_ms: int
    event_type: str
    confidence: float
    rationale: str
    implicated_target_ids: list[str]
    clip_uri: str | None
    model_version: str | None
    latitude: float | None
    longitude: float | None


def decode_scene_event(raw: bytes) -> DecodedSceneEvent:
    event = scene_pb.SceneEvent()
    event.ParseFromString(raw)

    # latitude/longitude are plain proto3 scalars, not `optional`, so there is
    # no HasField to check. (0.0, 0.0) is null island - no camera in this
    # fleet is there - so it stands in for "position unknown" the same way an
    # unset GeoLocation would.
    has_position = event.latitude != 0.0 or event.longitude != 0.0
    return DecodedSceneEvent(
        scene_event_id=event.scene_event_id,
        camera_id=event.camera_id,
        window_start_utc_ms=int(event.window_start_utc_ms),
        window_end_utc_ms=int(event.window_end_utc_ms),
        event_type=scene_pb.SceneEventType.Name(event.event_type),
        confidence=float(event.confidence),
        rationale=event.rationale,
        implicated_target_ids=list(event.implicated_target_ids),
        clip_uri=event.clip_uri or None,
        model_version=event.model_version or None,
        latitude=event.latitude if has_position else None,
        longitude=event.longitude if has_position else None,
    )


class SceneEventWorker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._consumer: Consumer | None = None
        self._publisher: httpx.AsyncClient | None = None
        self._stop = asyncio.Event()

        self.processed = 0
        self.rejected_identifiers = 0
        self.alerts_published = 0

    async def start(self) -> None:
        await database.connect(self.settings)

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
                "group.id": self.settings.kafka_scene_events_consumer_group,
                "auto.offset.reset": "earliest",
                "enable.auto.commit": False,
                "socket.keepalive.enable": True,
            }
        )
        self._consumer.subscribe([self.settings.kafka_topic_scene_events])

        logger.info(
            "scene event worker started",
            extra={"topic": self.settings.kafka_topic_scene_events},
        )

    async def stop(self) -> None:
        self._stop.set()

        if self._consumer is not None:
            consumer, self._consumer = self._consumer, None
            await asyncio.to_thread(consumer.close)

        if self._publisher is not None:
            publisher, self._publisher = self._publisher, None
            await publisher.aclose()

        await database.disconnect()
        logger.info(
            "scene event worker stopped",
            extra={
                "processed": self.processed,
                "rejected_identifiers": self.rejected_identifiers,
                "alerts_published": self.alerts_published,
            },
        )

    async def consume_forever(self) -> None:
        assert self._consumer is not None

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
                logger.exception("scene event processing failed")

            try:
                await asyncio.to_thread(
                    functools.partial(
                        self._consumer.commit, message=message, asynchronous=False
                    )
                )
            except KafkaException as exc:
                logger.warning("offset commit failed", extra={"error": str(exc)})

    async def _handle_message(self, raw: bytes | None) -> None:
        if not raw:
            return

        event = decode_scene_event(raw)

        scene_event_uuid = _as_uuid(event.scene_event_id)
        camera_uuid = _as_uuid(event.camera_id)
        if scene_event_uuid is None or camera_uuid is None:
            self.rejected_identifiers += 1
            logger.warning(
                "discarding scene event with unusable identifiers",
                extra={"scene_event_id": event.scene_event_id, "camera_id": event.camera_id},
            )
            return

        stored = await self._persist(event, scene_event_uuid, camera_uuid)
        self.processed += 1

        if stored and event.event_type in _ALERTABLE_EVENT_TYPES and (
            event.confidence >= self.settings.vlm_agent_alert_confidence_threshold
        ):
            await self._publish_alert(event)

        logger.info(
            "scene event processed",
            extra={
                "scene_event_id": event.scene_event_id,
                "camera_id": event.camera_id,
                "event_type": event.event_type,
                "confidence": round(event.confidence, 3),
                "stored": stored,
            },
        )

    async def _persist(
        self, event: DecodedSceneEvent, scene_event_uuid: uuid.UUID, camera_uuid: uuid.UUID
    ) -> bool:
        pool = database.get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                _INSERT_SCENE_EVENT_SQL,
                scene_event_uuid,
                camera_uuid,
                event.window_start_utc_ms,
                event.window_end_utc_ms,
                event.event_type,
                event.confidence,
                event.rationale,
                event.implicated_target_ids,
                event.clip_uri,
                event.model_version,
                event.longitude,  # $11 -> ST_MakePoint X
                event.latitude,  # $12 -> ST_MakePoint Y
            )
        return row is not None

    async def _publish_alert(self, event: DecodedSceneEvent) -> None:
        """Fan out a high-severity scene finding through the existing alert path.

        Reuses ThreatAlert's shape and workers/handoff_worker.py's fan-out
        endpoint so there is exactly one alert UI, rather than a second one
        this worker would otherwise need to invent.
        """
        if self._publisher is None:
            return

        alert = ThreatAlert(
            alert_id=build_alert_id(
                None, event.camera_id, event.window_start_utc_ms, event.event_type
            ),
            priority="P1",
            classification=event.event_type,
            plate_number=None,
            camera_id=event.camera_id,
            latitude=event.latitude,
            longitude=event.longitude,
            detected_at=event.window_start_utc_ms,
            dispatched_at=now_epoch_ms(),
            confidence=event.confidence,
            vehicle={},
            subject={},
            evidence={
                "source": "VLM_AGENT",
                "scene_event_id": event.scene_event_id,
                "clip_uri": event.clip_uri,
                "rationale": event.rationale,
                "implicated_target_ids": event.implicated_target_ids,
            },
        )

        try:
            response = await self._publisher.post(
                self.settings.alert_publish_url, json=alert.to_dict()
            )
        except httpx.HTTPError as exc:
            logger.error("alert publish failed", extra={"error": str(exc)})
            return

        if response.status_code >= 400:
            logger.error("alert publish rejected", extra={"status_code": response.status_code})
            return

        self.alerts_published += 1


def _as_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def configure_logging(log_level: str) -> None:
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)


async def run() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)

    worker = SceneEventWorker(settings)
    await worker.start()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, worker._stop.set)

    consumer_task = asyncio.create_task(worker.consume_forever(), name="kafka-consumer")

    def _task_died(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.critical(
                "scene event worker task exited unexpectedly; shutting down",
                exc_info=exc,
            )
        worker._stop.set()

    consumer_task.add_done_callback(_task_died)

    try:
        await worker._stop.wait()
    except KeyboardInterrupt:
        worker._stop.set()
    finally:
        consumer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer_task
        await worker.stop()

    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
