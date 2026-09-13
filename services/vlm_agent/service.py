"""VLM agent service: vehicle attribute tagging (Tier A) and gated scene/event
reasoning (Tier B).

Run with:
    python -m services.vlm_agent

Tier A is a Kafka consumer of ``surveillance-events-raw``, filtered to vehicle
object classes: for each qualifying detection it fetches the snapshot ANPR
already wrote, runs one local VLM call, and republishes a correlated
SurveillanceEvent carrying only ``color``/``make``/``model``/``vehicle_type``
attributes. It never re-derives frames itself and never runs the model on a
raw frame - cost rides entirely on ANPR's own detection cadence, the same
"cheap gate, then one bounded VLM call" shape services/anpr uses for its
motion gate before the plate detector.

Tier B (see agent_loop.py) is a periodic trigger scan against the detections
already persisted by workers/handoff_worker.py: only a camera that trips a
cheap, DB-derivable condition (a Re-ID target dwelling past a threshold, or
too many distinct targets in one camera's recent window) gets a buffered clip
and a single, more expensive reasoning call. Its own bounded queue and
concurrency limit are separate from Tier A's, so a burst of triggers can never
starve vehicle-attribute tagging, and vice versa.

Both tiers share one Kafka producer and one VLMClient (the model is loaded
once, the expensive part, and reused for every call of either kind).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any, Final

from app import database
from app.config import Settings, get_settings
from app.main import JsonLogFormatter
from services.vlm_agent.agent_loop import SceneFinding, Trigger, TriggerEngine, process_trigger
from services.vlm_agent.vlm_client import VehicleTags, VLMClient

try:
    from confluent_kafka import Consumer, KafkaError, Producer
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit(
        "confluent-kafka is not installed; run: pip install -r requirements.txt"
    ) from exc

try:
    from generated import scene_event_pb2 as scene_pb
    from generated import surveillance_event_pb2 as pb
except ImportError as exc:  # pragma: no cover - codegen guard
    raise SystemExit(
        "generated proto bindings are missing. Run scripts\\gen_proto.ps1"
    ) from exc

logger: Final = logging.getLogger("services.vlm_agent")

# Object classes Tier A tags. Pedestrians and UNKNOWN carry no make/model/color
# worth asking a VLM about.
_VEHICLE_CLASSES: Final = frozenset({"AUTOMOBILE", "MOTORCYCLE", "TRUCK", "BUS"})


@dataclass
class TierACandidate:
    """One decoded detection queued for vehicle attribute tagging."""

    event_id: str
    camera_id: str
    department_tag: int
    object_class: str
    snapshot_uri: str


@dataclass
class Counters:
    tier_a_seen: int = 0
    tier_a_skipped_own_tag: int = 0
    tier_a_skipped_no_snapshot: int = 0
    tier_a_dropped_queue_full: int = 0
    tier_a_tagged: int = 0
    tier_a_failed: int = 0
    tier_b_scans: int = 0
    tier_b_triggers: int = 0
    tier_b_findings: int = 0
    tier_b_published: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "tier_a_seen": self.tier_a_seen,
            "tier_a_skipped_own_tag": self.tier_a_skipped_own_tag,
            "tier_a_skipped_no_snapshot": self.tier_a_skipped_no_snapshot,
            "tier_a_dropped_queue_full": self.tier_a_dropped_queue_full,
            "tier_a_tagged": self.tier_a_tagged,
            "tier_a_failed": self.tier_a_failed,
            "tier_b_scans": self.tier_b_scans,
            "tier_b_triggers": self.tier_b_triggers,
            "tier_b_findings": self.tier_b_findings,
            "tier_b_published": self.tier_b_published,
        }


class VlmAgentService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.counters = Counters()

        self._vlm: VLMClient | None = None
        self._producer: Producer | None = None
        self._tier_a_consumer: Consumer | None = None
        self._tier_a_queue: asyncio.Queue[TierACandidate] = asyncio.Queue(
            maxsize=settings.vlm_agent_tier_a_queue_size
        )
        self._trigger_engine = TriggerEngine(settings)
        # Bounds concurrent Tier B reasoning calls so a burst of triggers
        # cannot pile up unbounded VLM inference work.
        self._tier_b_semaphore = asyncio.Semaphore(settings.vlm_agent_tier_b_queue_size)
        self._stop = asyncio.Event()

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        await database.connect(self.settings)

        self._vlm = await asyncio.to_thread(
            VLMClient,
            self.settings.vlm_agent_model_path,
            self.settings.vlm_agent_mmproj_path,
            context_tokens=self.settings.vlm_agent_context_tokens,
            backend=self.settings.vlm_agent_backend,
            hosted_api_base_url=self.settings.vlm_agent_hosted_api_base_url,
            hosted_api_key=self.settings.vlm_agent_hosted_api_key,
            hosted_model_name=self.settings.vlm_agent_hosted_model_name,
            hosted_request_timeout_seconds=self.settings.vlm_agent_hosted_request_timeout_seconds,
        )

        self._producer = Producer(
            {
                "bootstrap.servers": self.settings.kafka_bootstrap_servers,
                "linger.ms": 50,
                "compression.type": "snappy",
                "socket.keepalive.enable": True,
            }
        )

        self._tier_a_consumer = Consumer(
            {
                "bootstrap.servers": self.settings.kafka_bootstrap_servers,
                "group.id": self.settings.vlm_agent_kafka_consumer_group,
                "auto.offset.reset": "latest",
                # Committed only after the candidate is queued or explicitly
                # skipped - not after tagging completes - so a crash mid-batch
                # replays a detection rather than silently never tagging it.
                # A replayed tag is a harmless re-publish: the merge in
                # workers/handoff_worker.py is an idempotent attribute set.
                "enable.auto.commit": False,
                "socket.keepalive.enable": True,
            }
        )
        self._tier_a_consumer.subscribe([self.settings.kafka_topic_raw])

        logger.info(
            "vlm agent service started",
            extra={
                "tier_a_topic": self.settings.kafka_topic_raw,
                "tier_b_topic": self.settings.kafka_topic_scene_events,
                "hosted_escalation": self._vlm.hosted_escalation_available,
            },
        )

    async def stop(self) -> None:
        self._stop.set()

        if self._tier_a_consumer is not None:
            consumer, self._tier_a_consumer = self._tier_a_consumer, None
            await asyncio.to_thread(consumer.close)

        if self._producer is not None:
            producer, self._producer = self._producer, None
            remaining = await asyncio.to_thread(producer.flush, 5.0)
            if remaining:
                logger.warning("%d event(s) undelivered at shutdown", remaining)

        await database.disconnect()
        logger.info("vlm agent service stopped", extra=self.counters.snapshot())

    # -- Tier A: consume, tag, republish -------------------------------------

    async def tier_a_consume_loop(self) -> None:
        assert self._tier_a_consumer is not None

        while not self._stop.is_set():
            message = await asyncio.to_thread(
                self._tier_a_consumer.poll, self.settings.kafka_poll_timeout_seconds
            )
            if message is None:
                continue

            error = message.error()
            if error is not None:
                if error.code() == KafkaError._PARTITION_EOF:
                    continue
                logger.error("tier a kafka error", extra={"error": str(error)})
                continue

            try:
                self._enqueue_candidate(message.value())
            except Exception:
                logger.exception("failed to decode tier a candidate")

            # Keyword arguments are load-bearing here, same footgun documented
            # in workers/handoff_worker.py: Consumer.commit(msg, False) binds
            # positionally to the wrong parameter and raises TypeError, which
            # is not a KafkaException and would otherwise escape and kill this
            # loop while the rest of the service keeps running.
            try:
                await asyncio.to_thread(
                    functools.partial(
                        self._tier_a_consumer.commit, message=message, asynchronous=False
                    )
                )
            except Exception:
                logger.warning("tier a offset commit failed")

    def _enqueue_candidate(self, raw: bytes | None) -> None:
        if not raw:
            return

        event = pb.SurveillanceEvent()
        event.ParseFromString(raw)

        # Never tag the service's own previously-published tagging events:
        # they carry attributes["source"] == "VLM_AGENT" and no snapshot_uri
        # of their own. Without this guard the consumer would loop on its own
        # output forever, since it reads the same topic it publishes onto.
        if event.attributes.get("source") == "VLM_AGENT":
            self.counters.tier_a_skipped_own_tag += 1
            return

        object_class = pb.ObjectClass.Name(event.object_class)
        if object_class not in _VEHICLE_CLASSES:
            return

        self.counters.tier_a_seen += 1

        snapshot_uri = event.attributes.get("snapshot_uri", "")
        if not snapshot_uri:
            self.counters.tier_a_skipped_no_snapshot += 1
            return

        candidate = TierACandidate(
            event_id=event.event_id,
            camera_id=event.camera_id,
            department_tag=int(event.department_code),
            object_class=object_class,
            snapshot_uri=snapshot_uri,
        )
        try:
            self._tier_a_queue.put_nowait(candidate)
        except asyncio.QueueFull:
            # Same backpressure posture as services/anpr: drop the newest
            # candidate and count it rather than blocking the consumer, which
            # would eventually stall Kafka's own internal buffers.
            self.counters.tier_a_dropped_queue_full += 1

    async def tier_a_inference_loop(self) -> None:
        assert self._vlm is not None
        import cv2

        while not self._stop.is_set():
            try:
                candidate = await asyncio.wait_for(self._tier_a_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            try:
                image = await asyncio.to_thread(cv2.imread, candidate.snapshot_uri)
                if image is None:
                    self.counters.tier_a_failed += 1
                    logger.warning(
                        "snapshot unreadable; skipping tag",
                        extra={"event_id": candidate.event_id, "snapshot_uri": candidate.snapshot_uri},
                    )
                    continue

                tags = await asyncio.to_thread(self._vlm.tag_vehicle, image)
                if tags is None:
                    self.counters.tier_a_failed += 1
                    continue

                self._publish_tier_a(candidate, tags)
                self.counters.tier_a_tagged += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                self.counters.tier_a_failed += 1
                logger.exception("tier a tagging failed", extra={"event_id": candidate.event_id})
            finally:
                self._tier_a_queue.task_done()

    def _publish_tier_a(self, candidate: TierACandidate, tags: VehicleTags) -> None:
        assert self._producer is not None

        event = pb.SurveillanceEvent(
            event_id=str(uuid.uuid4()),
            camera_id=candidate.camera_id,
            department_code=candidate.department_tag,
            timestamp_utc_ms=int(time.time() * 1000),
            object_class=pb.ObjectClass.Value(candidate.object_class),
        )
        event.attributes["source"] = "VLM_AGENT"
        event.attributes["source_event_id"] = candidate.event_id
        event.attributes["color"] = tags.color
        event.attributes["make"] = tags.make
        event.attributes["model"] = tags.model
        event.attributes["vehicle_type"] = tags.vehicle_type

        try:
            self._producer.produce(
                self.settings.kafka_topic_raw,
                key=str(candidate.department_tag),
                value=event.SerializeToString(),
                headers=[("event_id", event.event_id), ("source_event_id", candidate.event_id)],
            )
            self._producer.poll(0)
        except BufferError:
            logger.warning("kafka producer queue full; vehicle tag dropped")
        except Exception:
            logger.exception("failed to publish vehicle tag")

    # -- Tier B: trigger scan, capture, reason, publish ----------------------

    async def tier_b_scan_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.settings.vlm_agent_trigger_scan_seconds)
            try:
                triggers = await self._trigger_engine.scan()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("tier b trigger scan failed")
                continue

            self.counters.tier_b_scans += 1
            if not triggers:
                continue

            self.counters.tier_b_triggers += len(triggers)
            for trigger in triggers:
                asyncio.create_task(
                    self._run_trigger(trigger), name=f"tier-b-{trigger.camera_id}"
                )

    async def _run_trigger(self, trigger: Trigger) -> None:
        assert self._vlm is not None

        async with self._tier_b_semaphore:
            try:
                produced = await process_trigger(
                    trigger, self.settings, self._vlm, self._publish_tier_b
                )
                if produced:
                    self.counters.tier_b_findings += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "tier b trigger processing failed",
                    extra={"camera_id": trigger.camera_id, "hint": trigger.hint},
                )

    def _publish_tier_b(
        self,
        trigger: Trigger,
        finding: SceneFinding,
        window: tuple[int, int],
        position: tuple[float, float] | None,
    ) -> None:
        assert self._producer is not None

        window_start_ms, window_end_ms = window
        scene_event = scene_pb.SceneEvent(
            scene_event_id=str(uuid.uuid4()),
            camera_id=trigger.camera_id,
            window_start_utc_ms=window_start_ms,
            window_end_utc_ms=window_end_ms,
            event_type=scene_pb.SceneEventType.Value(finding.event_type),
            confidence=finding.confidence,
            rationale=finding.rationale,
            model_version=self._model_version_label(),
        )
        if position is not None:
            scene_event.latitude, scene_event.longitude = position

        try:
            self._producer.produce(
                self.settings.kafka_topic_scene_events,
                key=trigger.camera_id,
                value=scene_event.SerializeToString(),
                headers=[("scene_event_id", scene_event.scene_event_id)],
            )
            self._producer.poll(0)
            self.counters.tier_b_published += 1
        except BufferError:
            logger.warning("kafka producer queue full; scene event dropped")
        except Exception:
            logger.exception("failed to publish scene event")

    def _model_version_label(self) -> str:
        """Which VLM build/endpoint actually produced a Tier B finding.

        Recorded on every SceneEvent for audit and for rolling back a prompt
        or backend change that starts raising bad findings, per the proto's
        own doc comment on `model_version`.
        """
        if self.settings.vlm_agent_backend == "hosted":
            return f"hosted:{self.settings.vlm_agent_hosted_model_name}"
        return f"local:{self.settings.vlm_agent_model_path}"

    # -- periodic reporting ---------------------------------------------------

    async def maintenance_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.settings.vlm_agent_report_seconds)
            logger.info(
                "vlm agent throughput",
                extra={
                    **self.counters.snapshot(),
                    "tier_a_queue_depth": self._tier_a_queue.qsize(),
                },
            )


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

    service = VlmAgentService(settings)
    await service.start()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, service._stop.set)

    tasks = [
        asyncio.create_task(service.tier_a_consume_loop(), name="tier-a-consume"),
        asyncio.create_task(service.tier_a_inference_loop(), name="tier-a-inference"),
        asyncio.create_task(service.tier_b_scan_loop(), name="tier-b-scan"),
        asyncio.create_task(service.maintenance_loop(), name="maintenance"),
    ]

    def _task_died(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.critical(
                "vlm agent task exited unexpectedly; shutting down",
                extra={"task": task.get_name()},
                exc_info=exc,
            )
            service._stop.set()

    for task in tasks:
        task.add_done_callback(_task_died)

    try:
        await service._stop.wait()
    except KeyboardInterrupt:
        service._stop.set()
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await service.stop()

    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
