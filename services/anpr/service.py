"""ANPR service: sample live cameras, read plates, publish to the analytics bus.

Run with:
    python -m services.anpr

Shape of the thing: K grabber tasks pull frames from cameras into a bounded
queue, and one inference task drains it. That split is deliberate.

* Grabbing is IO-bound - waiting on RTSP - so several can overlap.
* Inference is CPU-bound and ONNX Runtime already uses every core internally, so
  running several at once would thrash rather than parallelise.
* The bounded queue is the backpressure. When inference cannot keep up the queue
  fills, grabbers drop frames and count them, and the service reports a lower
  achieved rate. It never silently falls further and further behind.

Each grabber holds a camera for a short *dwell* rather than taking single shots:
an RTSP handshake plus keyframe wait costs far more than a frame, so paying it
once per several frames is what makes the sampling affordable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

import asyncpg
import cv2
import numpy as np

from app import database
from app.config import Settings, get_settings
from app.main import JsonLogFormatter
from app.routers.streams import _with_grid_credentials
from services.anpr.reader import PlateReader, PlateReading

try:
    from confluent_kafka import Producer
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit(
        "confluent-kafka is not installed; run: pip install -r requirements.txt"
    ) from exc

try:
    from generated import surveillance_event_pb2 as pb
except ImportError as exc:  # pragma: no cover - codegen guard
    raise SystemExit(
        "generated/surveillance_event_pb2.py is missing. Run scripts\\gen_proto.ps1"
    ) from exc

logger: Final = logging.getLogger("services.anpr")

_LOAD_CAMERAS_SQL: Final = """
    SELECT id::text AS camera_id,
           global_camera_code,
           department_id,
           camera_type,
           stream_url,
           ST_Y(location_geom) AS latitude,
           ST_X(location_geom) AS longitude,
           azimuth_angle
    FROM cameras
    WHERE status = 'ACTIVE'
      AND stream_url IS NOT NULL
    ORDER BY
        -- Cameras built for plate capture come first, so that when the budget
        -- is tight they are the ones that keep their fast interval.
        CASE WHEN camera_type = 'ANPR' THEN 0 ELSE 1 END,
        global_camera_code
"""


@dataclass
class Camera:
    """A camera the service samples, plus its schedule."""

    camera_id: str
    global_camera_code: str
    department_id: str | None
    camera_type: str | None
    stream_url: str
    latitude: float | None
    longitude: float | None
    azimuth_degrees: float | None
    interval_seconds: float
    due_at: float = 0.0
    #: Consecutive grab failures; drives exponential backoff on a dead camera.
    failures: int = 0

    @property
    def is_anpr(self) -> bool:
        return self.camera_type == "ANPR"


@dataclass
class Frame:
    """One sampled frame awaiting inference."""

    camera: Camera
    image: np.ndarray
    captured_utc_ms: int


@dataclass
class Counters:
    """What the service actually did, so the real rate is observable."""

    frames_grabbed: int = 0
    frames_gated: int = 0
    frames_dropped_queue_full: int = 0
    inferences: int = 0
    plates_read: int = 0
    plates_published: int = 0
    plates_deduped: int = 0
    plates_low_confidence: int = 0
    grab_failures: int = 0
    inference_ms_total: float = 0.0

    def snapshot(self) -> dict[str, Any]:
        mean_ms = (
            round(self.inference_ms_total / self.inferences, 1) if self.inferences else 0.0
        )
        return {
            "frames_grabbed": self.frames_grabbed,
            "frames_gated": self.frames_gated,
            "frames_dropped_queue_full": self.frames_dropped_queue_full,
            "inferences": self.inferences,
            "mean_inference_ms": mean_ms,
            "plates_read": self.plates_read,
            "plates_published": self.plates_published,
            "plates_deduped": self.plates_deduped,
            "plates_low_confidence": self.plates_low_confidence,
            "grab_failures": self.grab_failures,
        }


class AnprService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.counters = Counters()

        self._cameras: list[Camera] = []
        self._cameras_lock = asyncio.Lock()
        # ANPR-designated cameras get a dedicated continuous-capture task rather
        # than competing for a shared grabber slot. Keyed by camera_id, holding
        # (task, camera-as-last-started) so a stream_url change is detected and
        # the task restarted rather than silently dialling a stale URL forever.
        self._dedicated_tasks: dict[str, tuple[asyncio.Task[None], Camera]] = {}
        self._queue: asyncio.Queue[Frame] = asyncio.Queue(
            maxsize=settings.anpr_queue_size
        )
        self._stop = asyncio.Event()
        self._reader: PlateReader | None = None
        self._producer: Producer | None = None

        # (plate, camera_id) -> monotonic time last published. Stops one vehicle
        # dwelling at a junction from filling the bus with the same reading.
        self._recent: dict[tuple[str, str], float] = {}

        self._snapshot_dir = Path(settings.anpr_snapshot_dir)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        await database.connect(self.settings)

        # Loading the models takes a few seconds and allocates the ONNX
        # sessions; do it before any camera is scheduled.
        self._reader = await asyncio.to_thread(
            PlateReader,
            self.settings.anpr_detector_model,
            self.settings.anpr_ocr_model,
            min_detection_confidence=self.settings.anpr_min_detection_confidence,
            motion_threshold=self.settings.anpr_motion_threshold,
        )

        self._producer = Producer(
            {
                "bootstrap.servers": self.settings.kafka_bootstrap_servers,
                "linger.ms": 50,
                "compression.type": "snappy",
                "socket.keepalive.enable": True,
            }
        )

        if self.settings.anpr_save_snapshots:
            self._snapshot_dir.mkdir(parents=True, exist_ok=True)

        await self._reload_cameras()

        logger.info(
            "anpr service started",
            extra={
                "cameras": len(self._cameras),
                "anpr_cameras": sum(1 for c in self._cameras if c.is_anpr),
                "grabbers": self.settings.anpr_grabbers,
                "detector": self.settings.anpr_detector_model,
                "ocr": self.settings.anpr_ocr_model,
                "topic": self.settings.kafka_topic_raw,
            },
        )

    async def stop(self) -> None:
        self._stop.set()

        for task, _camera in list(self._dedicated_tasks.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._dedicated_tasks.clear()

        if self._producer is not None:
            producer, self._producer = self._producer, None
            # Give queued readings a chance to reach the broker; anything still
            # outstanding after the timeout is reported rather than dropped
            # quietly.
            remaining = await asyncio.to_thread(producer.flush, 5.0)
            if remaining:
                logger.warning("%d plate event(s) undelivered at shutdown", remaining)

        await database.disconnect()
        logger.info("anpr service stopped", extra=self.counters.snapshot())

    # -- camera scheduling -------------------------------------------------

    async def _reload_cameras(self) -> None:
        """Rebuild the camera list, preserving each camera's schedule.

        Built off to the side and swapped under the lock: a failed reload keeps
        the previous list rather than emptying it, because an empty list stops
        the service silently instead of failing loudly.
        """
        try:
            pool = database.get_pool()
            async with pool.acquire() as connection:
                rows = await connection.fetch(_LOAD_CAMERAS_SQL)
        except (asyncpg.PostgresError, RuntimeError):
            logger.exception("camera reload failed; keeping the previous list")
            return

        existing = {camera.camera_id: camera for camera in self._cameras}
        rebuilt: list[Camera] = []

        for row in rows:
            record = dict(row)
            is_anpr = record.get("camera_type") == "ANPR"
            interval = (
                self.settings.anpr_fast_interval_seconds
                if is_anpr
                else self.settings.anpr_slow_interval_seconds
            )
            previous = existing.get(record["camera_id"])
            rebuilt.append(
                Camera(
                    camera_id=record["camera_id"],
                    global_camera_code=record["global_camera_code"],
                    department_id=record["department_id"],
                    camera_type=record["camera_type"],
                    stream_url=record["stream_url"],
                    latitude=record["latitude"],
                    longitude=record["longitude"],
                    azimuth_degrees=(
                        float(record["azimuth_angle"])
                        if record["azimuth_angle"] is not None
                        else None
                    ),
                    interval_seconds=interval,
                    # Carry the schedule across a reload, or every reload would
                    # make every camera due at once and stampede the grabbers.
                    due_at=previous.due_at if previous else 0.0,
                    failures=previous.failures if previous else 0,
                )
            )

        async with self._cameras_lock:
            self._cameras = rebuilt

        await self._sync_dedicated_tasks(rebuilt)

        logger.info("camera list refreshed", extra={"cameras": len(rebuilt)})

    async def _next_due_camera(self) -> Camera | None:
        """Claim the patrol camera that has been waiting longest past its interval.

        ANPR-designated cameras are excluded: they are held by a dedicated
        continuous-capture task and must never also be claimed here, or the
        same stream would be dialled twice.
        """
        now = time.monotonic()
        async with self._cameras_lock:
            due = [c for c in self._cameras if c.due_at <= now and not c.is_anpr]
            if not due:
                return None
            camera = min(due, key=lambda c: c.due_at)
            # Reserve it immediately so no other grabber claims the same camera.
            camera.due_at = now + camera.interval_seconds
            return camera

    def _dial_url(self, camera: Camera) -> str:
        """The URL ffmpeg should open for this camera.

        Prefers MediaMTX, which already holds the camera's connection and needs
        no credentials. Otherwise dials the camera directly, injecting the grid
        credentials through the same seam the WebRTC proxy uses - registry rows
        deliberately do not store them.
        """
        parsed = urlparse(camera.stream_url)
        host = (parsed.hostname or "").lower()
        media_host, _, media_port = self.settings.mediamtx_rtsp_host.partition(":")

        if host in {media_host.lower(), "127.0.0.1", "localhost", "mediamtx"}:
            return camera.stream_url

        return _with_grid_credentials(camera.stream_url, self.settings)

    # -- frame grabbing ----------------------------------------------------

    def _ffmpeg_command(self, camera: Camera, *, duration_seconds: float | None) -> list[str]:
        """Build the ffmpeg invocation for one camera.

        ffmpeg does the decoding and scaling: handing it `-s` costs nothing here
        and avoids piping full-resolution frames we would immediately shrink.
        `duration_seconds=None` means "run until the process is killed" - the
        dedicated ANPR loop's continuous capture; a value is the patrol pool's
        bounded dwell.
        """
        width = self.settings.anpr_frame_width
        height = self.settings.anpr_frame_height
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", self._dial_url(camera),
            "-an",
            "-vf", f"fps={self.settings.anpr_dwell_fps}",
            "-s", f"{width}x{height}",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
        ]
        if duration_seconds is not None:
            command += ["-t", str(duration_seconds)]
        command.append("pipe:1")
        return command

    async def _pump_frames(
        self,
        camera: Camera,
        process: asyncio.subprocess.Process,
        *,
        deadline: float | None,
    ) -> int:
        """Read raw frames from an open ffmpeg process into the shared queue.

        Shared by the patrol dwell and the ANPR dedicated loop: only how long a
        grabber keeps the process open differs (a bounded dwell vs. forever),
        not how a frame becomes a `Frame` and reaches inference. Returns the
        count read, so the caller can tell a live camera from a dead one.
        """
        width = self.settings.anpr_frame_width
        height = self.settings.anpr_frame_height
        frame_bytes = width * height * 3
        grabbed = 0

        while not self._stop.is_set() and (deadline is None or time.monotonic() < deadline):
            try:
                raw = await asyncio.wait_for(
                    process.stdout.readexactly(frame_bytes),
                    timeout=self.settings.anpr_grab_timeout_seconds,
                )
            except asyncio.IncompleteReadError:
                break  # stream ended - the normal end of a dwell, or a dropped connection
            except asyncio.TimeoutError:
                logger.debug("grab timed out", extra={"camera": camera.global_camera_code})
                break

            image = np.frombuffer(raw, dtype=np.uint8).reshape((height, width, 3))
            grabbed += 1
            self.counters.frames_grabbed += 1

            frame = Frame(camera=camera, image=image, captured_utc_ms=int(time.time() * 1000))
            try:
                self._queue.put_nowait(frame)
            except asyncio.QueueFull:
                # This is the backpressure. Dropping the newest frame is right:
                # the queued ones are older and closer to being stale, and
                # holding the grabber here would stall the camera.
                self.counters.frames_dropped_queue_full += 1

        return grabbed

    async def _grab_dwell(self, camera: Camera) -> int:
        """Pull frames from one patrol camera for its dwell window. Returns the count."""
        command = self._ffmpeg_command(camera, duration_seconds=self.settings.anpr_dwell_seconds)
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        deadline = time.monotonic() + self.settings.anpr_dwell_seconds + 10.0
        try:
            grabbed = await self._pump_frames(camera, process, deadline=deadline)
        finally:
            if process.returncode is None:
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()

        if grabbed == 0:
            camera.failures += 1
            self.counters.grab_failures += 1
            stderr = b""
            with contextlib.suppress(Exception):
                stderr = await process.stderr.read()
            # Back off a failing camera rather than retrying it every interval:
            # a dead camera would otherwise consume a grabber slot continuously.
            backoff = min(
                camera.interval_seconds * (2**camera.failures),
                self.settings.anpr_max_backoff_seconds,
            )
            camera.due_at = time.monotonic() + backoff
            logger.warning(
                "no frames from camera",
                extra={
                    "camera": camera.global_camera_code,
                    "failures": camera.failures,
                    "backoff_seconds": round(backoff, 1),
                    "ffmpeg": stderr.decode("utf-8", "replace")[:200].strip(),
                },
            )
        else:
            camera.failures = 0

        return grabbed

    async def grabber_loop(self, slot: int) -> None:
        """One shared-pool slot: claim a due patrol camera, dwell on it, repeat.

        ANPR-designated cameras never appear here - they hold a dedicated task
        (`_dedicated_capture_loop`) instead of competing for one of these slots.
        """
        while not self._stop.is_set():
            camera = await self._next_due_camera()
            if camera is None:
                await asyncio.sleep(0.25)
                continue

            try:
                await self._grab_dwell(camera)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "grabber failed", extra={"slot": slot, "camera": camera.global_camera_code}
                )

    async def _dedicated_capture_loop(self, camera: Camera) -> None:
        """Hold one ANPR-designated camera open continuously.

        This is the guarantee the shared round-robin pool cannot make: a
        camera in `grabber_loop`'s pool is released after one dwell and may
        wait a full interval before it is watched again, so a vehicle passing
        in that gap is never read. A dedicated camera has no gap - its ffmpeg
        process stays open for as long as the service runs, reconnecting with
        the same exponential backoff as a patrol camera only when the stream
        itself fails.

        Frames still flow through the same bounded queue and the same
        motion-gated inference path as everything else; only the grab loop
        differs; nothing about detection, OCR or publishing is duplicated.
        """
        while not self._stop.is_set():
            try:
                command = self._ffmpeg_command(camera, duration_seconds=None)
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    grabbed = await self._pump_frames(camera, process, deadline=None)
                finally:
                    if process.returncode is None:
                        process.kill()
                    with contextlib.suppress(Exception):
                        await process.wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A bug here must not silently end continuous coverage of a
                # designated capture point - log it and keep retrying rather
                # than letting the task die.
                logger.exception(
                    "dedicated capture loop failed",
                    extra={"camera": camera.global_camera_code},
                )
                grabbed = 0

            if self._stop.is_set():
                break

            if grabbed == 0:
                camera.failures += 1
                self.counters.grab_failures += 1
                backoff = min(
                    self.settings.anpr_fast_interval_seconds * (2**camera.failures),
                    self.settings.anpr_max_backoff_seconds,
                )
                logger.warning(
                    "dedicated capture reconnecting after no frames",
                    extra={
                        "camera": camera.global_camera_code,
                        "failures": camera.failures,
                        "backoff_seconds": round(backoff, 1),
                    },
                )
                await asyncio.sleep(backoff)
            else:
                camera.failures = 0
                # ffmpeg exited cleanly (e.g. its own reconnect boundary, or the
                # `-vf fps=` pipeline hitting EOF): reopen promptly rather than
                # waiting a full backoff for what is not a failure.
                await asyncio.sleep(0.5)

    async def _sync_dedicated_tasks(self, cameras: list[Camera]) -> None:
        """Start continuous capture for newly ANPR-tagged cameras, stop it for
        ones no longer tagged (or removed), restart it if a stream URL changed.
        """
        current = {c.camera_id: c for c in cameras if c.is_anpr}

        for camera_id in list(self._dedicated_tasks):
            task, running_camera = self._dedicated_tasks[camera_id]
            replacement = current.get(camera_id)
            if replacement is None or replacement.stream_url != running_camera.stream_url:
                del self._dedicated_tasks[camera_id]
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                logger.info(
                    "dedicated continuous capture stopped",
                    extra={"camera": running_camera.global_camera_code},
                )

        for camera_id, camera in current.items():
            if camera_id in self._dedicated_tasks:
                continue
            task = asyncio.create_task(
                self._dedicated_capture_loop(camera),
                name=f"anpr-dedicated-{camera.global_camera_code}",
            )
            self._dedicated_tasks[camera_id] = (task, camera)
            logger.info(
                "dedicated continuous capture started",
                extra={"camera": camera.global_camera_code},
            )

    # -- inference ---------------------------------------------------------

    async def inference_loop(self) -> None:
        """Drain the frame queue: gate on motion, then read plates."""
        assert self._reader is not None

        while not self._stop.is_set():
            try:
                frame = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            try:
                # The gate is ~100x cheaper than the detector, so it runs inline
                # rather than paying a thread hop to skip a frame.
                if not self._reader.has_motion(frame.camera.camera_id, frame.image):
                    self.counters.frames_gated += 1
                    continue

                started = time.perf_counter()
                # ONNX Runtime releases the GIL, so a thread genuinely offloads
                # this and keeps the grabbers responsive.
                readings = await asyncio.to_thread(self._reader.read, frame.image)
                self.counters.inferences += 1
                self.counters.inference_ms_total += (time.perf_counter() - started) * 1000

                for reading in readings:
                    self.counters.plates_read += 1
                    await self._publish(frame, reading)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("inference failed")
            finally:
                self._queue.task_done()

    # -- publishing --------------------------------------------------------

    def _should_publish(self, plate: str, camera_id: str) -> bool:
        key = (plate, camera_id)
        now = time.monotonic()
        last = self._recent.get(key)
        if last is not None and now - last < self.settings.anpr_dedup_seconds:
            return False

        self._recent[key] = now
        if len(self._recent) > 20_000:
            cutoff = now - self.settings.anpr_dedup_seconds
            self._recent = {k: v for k, v in self._recent.items() if v >= cutoff}
        return True

    def _save_snapshot(self, frame: Frame, reading: PlateReading) -> str | None:
        """Write the plate crop to disk as evidence; returns its path."""
        if not self.settings.anpr_save_snapshots:
            return None

        x1, y1, x2, y2 = reading.box
        crop = frame.image[y1:y2, x1:x2]
        if crop.size == 0:
            return None

        name = f"{reading.plate}_{frame.camera.global_camera_code}_{frame.captured_utc_ms}.jpg"
        path = self._snapshot_dir / name
        try:
            cv2.imwrite(str(path), crop)
        except Exception:
            logger.exception("failed to write snapshot")
            return None
        return str(path)

    async def _publish(self, frame: Frame, reading: PlateReading) -> None:
        """Publish one plate reading to the analytics bus."""
        if reading.confidence < self.settings.anpr_min_publish_confidence:
            self.counters.plates_low_confidence += 1
            return

        if not self._should_publish(reading.plate, frame.camera.camera_id):
            self.counters.plates_deduped += 1
            return

        snapshot = await asyncio.to_thread(self._save_snapshot, frame, reading)

        camera = frame.camera
        department = pb.DepartmentCode.Value("DEPARTMENT_CODE_UNSPECIFIED")
        if camera.department_id:
            with contextlib.suppress(ValueError):
                department = pb.DepartmentCode.Value(camera.department_id)

        event = pb.SurveillanceEvent(
            # A valid UUIDv4 is mandatory: the worker rejects and counts events
            # whose identifiers it cannot store, rather than letting them fail
            # inside the INSERT and disappear.
            event_id=str(uuid.uuid4()),
            camera_id=camera.camera_id,
            department_code=department,
            timestamp_utc_ms=frame.captured_utc_ms,
            object_class=pb.ObjectClass.Value("AUTOMOBILE"),
        )
        if camera.latitude is not None and camera.longitude is not None:
            event.location.latitude = camera.latitude
            event.location.longitude = camera.longitude
            if camera.azimuth_degrees is not None:
                event.location.azimuth_degrees = camera.azimuth_degrees

        height, width = frame.image.shape[:2]
        x1, y1, x2, y2 = reading.box
        # Normalised to the frame, as the contract specifies, so a box stays
        # meaningful across the mixed resolutions in the fleet.
        event.bounding_box.x_min = x1 / width
        event.bounding_box.y_min = y1 / height
        event.bounding_box.x_max = x2 / width
        event.bounding_box.y_max = y2 / height

        event.attributes["license_plate"] = reading.plate
        event.attributes["license_plate_conf"] = f"{reading.confidence:.3f}"
        event.attributes["source"] = "ANPR"
        if reading.corrected:
            # Keep what the OCR actually said. A corrected reading is an
            # inference, and the raw text is the evidence behind it.
            event.attributes["license_plate_raw"] = reading.raw
        if snapshot:
            event.attributes["snapshot_uri"] = snapshot

        # feature_embedding is deliberately empty: this producer reads plates
        # and has no re-identification model. The worker handles that.
        payload = event.SerializeToString()

        try:
            self._producer.produce(
                self.settings.kafka_topic_raw,
                key=str(int(department)),
                value=payload,
                headers=[("event_id", event.event_id), ("camera_id", camera.camera_id)],
            )
            self._producer.poll(0)
        except BufferError:
            logger.warning("kafka producer queue full; plate reading dropped")
            return
        except Exception:
            logger.exception("failed to publish plate reading")
            return

        self.counters.plates_published += 1
        logger.info(
            "plate read",
            extra={
                "plate": reading.plate,
                "raw": reading.raw if reading.corrected else None,
                "confidence": round(reading.confidence, 3),
                "camera": camera.global_camera_code,
            },
        )

    # -- periodic tasks ----------------------------------------------------

    async def maintenance_loop(self) -> None:
        """Refresh cameras and report the rate actually achieved."""
        last_reload = time.monotonic()
        last_report = time.monotonic()
        last_counts = self.counters.snapshot()

        while not self._stop.is_set():
            await asyncio.sleep(1.0)
            now = time.monotonic()

            if now - last_reload >= self.settings.anpr_camera_reload_seconds:
                await self._reload_cameras()
                last_reload = now

            if now - last_report >= self.settings.anpr_report_seconds:
                elapsed = now - last_report
                counts = self.counters.snapshot()
                logger.info(
                    "anpr throughput",
                    extra={
                        **counts,
                        "window_seconds": round(elapsed, 1),
                        "frames_per_second": round(
                            (counts["frames_grabbed"] - last_counts["frames_grabbed"]) / elapsed, 2
                        ),
                        "inferences_per_second": round(
                            (counts["inferences"] - last_counts["inferences"]) / elapsed, 2
                        ),
                        "queue_depth": self._queue.qsize(),
                    },
                )
                last_report = now
                last_counts = counts


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

    service = AnprService(settings)
    await service.start()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, service._stop.set)

    tasks = [
        asyncio.create_task(service.grabber_loop(i), name=f"grabber-{i}")
        for i in range(settings.anpr_grabbers)
    ]
    tasks.append(asyncio.create_task(service.inference_loop(), name="inference"))
    tasks.append(asyncio.create_task(service.maintenance_loop(), name="maintenance"))

    # No task may die quietly: a dead inference loop leaves a service that still
    # grabs frames and reads nothing.
    def _task_died(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.critical(
                "anpr task exited unexpectedly; shutting down",
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
