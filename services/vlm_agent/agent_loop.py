"""Tier B: per-camera trigger scan, gated clip capture, and VLM reasoning.

This is the actually agentic half of services/vlm_agent. Tier A (see
service.py) tags every vehicle detection; this module only acts when a cheap,
DB-derivable condition fires, and even then makes one VLM call per trigger -
never per frame, never on a fixed schedule per camera. Cost scales with
anomalies and activity, not with camera count times frame rate, mirroring
services/anpr/reader.py's motion-gate-before-detector shape.

WRONG_WAY and COLLISION are intentionally not implemented as trigger sources
here - see `TriggerEngine`'s docstring for why - but the VLM reasoning call is
always free to report either: `reason_about_clip`'s prompt asks it to decide
what is actually happening, not to confirm the trigger's hint.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Final

import numpy as np

from app import database
from app.config import Settings
from app.routers.streams import _with_grid_credentials
from services.vlm_agent.vlm_client import SceneFinding, VLMClient

try:
    from generated import scene_event_pb2 as scene_pb
except ImportError as exc:  # pragma: no cover - codegen guard
    raise SystemExit(
        "generated/scene_event_pb2.py is missing. Run scripts\\gen_proto.ps1"
    ) from exc

logger: Final = logging.getLogger("services.vlm_agent.agent_loop")

_LOITERING_CANDIDATES_SQL: Final = """
    SELECT camera_id, target_id,
           MIN(timestamp_utc_ms) AS first_seen_utc_ms,
           MAX(timestamp_utc_ms) AS last_seen_utc_ms
    FROM detections
    WHERE target_id IS NOT NULL
      AND timestamp_utc_ms >= $1
    GROUP BY camera_id, target_id
    HAVING MAX(timestamp_utc_ms) - MIN(timestamp_utc_ms) >= $2
"""

_CROWD_DENSITY_CANDIDATES_SQL: Final = """
    SELECT camera_id, COUNT(DISTINCT target_id) AS distinct_targets
    FROM detections
    WHERE target_id IS NOT NULL
      AND timestamp_utc_ms >= $1
    GROUP BY camera_id
    HAVING COUNT(DISTINCT target_id) >= $2
"""

_CAMERA_STREAM_SQL: Final = """
    SELECT stream_url,
           ST_Y(location_geom) AS latitude,
           ST_X(location_geom) AS longitude
    FROM cameras
    WHERE id = $1 AND status = 'ACTIVE' AND stream_url IS NOT NULL
"""

# Clip capture is deliberately smaller than ANPR's decode size: this is a
# scene-reasoning call, not a plate read, and a VLM's usable input resolution
# is far below 720p anyway - there is nothing to gain from paying to decode
# and pipe more than this.
_CLIP_FRAME_WIDTH: Final = 640
_CLIP_FRAME_HEIGHT: Final = 360
_CLIP_SAMPLE_FPS: Final = 1.0


@dataclass(frozen=True, slots=True)
class Trigger:
    camera_id: str
    hint: str  # lowercase label passed into the VLM prompt, e.g. "loitering"


class TriggerEngine:
    """Scans persisted detections for cheap, DB-derivable trigger conditions.

    Two sources are implemented against existing tables with no schema
    change: a Re-ID target dwelling past `vlm_agent_loitering_dwell_seconds`
    in one camera's detections (LOITERING candidate), and a camera seeing more
    than `vlm_agent_crowd_density_min_targets` distinct targets inside the
    scan lookback window (CROWD_DENSITY candidate).

    WRONG_WAY and COLLISION are NOT implemented as trigger sources: reliably
    detecting either needs a per-camera "expected traffic flow direction",
    which the registry does not model today (`cameras.azimuth_angle` is which
    way the camera faces, not which way traffic should move past it).
    Inventing that column was out of scope for this pass; the reasoning call
    itself can still surface either as its actual finding regardless of which
    hint triggered it.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # (camera_id, hint) -> monotonic time last triggered. Without this a
        # target that keeps loitering re-triggers the (expensive) reasoning
        # call on every scan tick rather than once per cooldown window.
        self._last_triggered: dict[tuple[str, str], float] = {}

    async def scan(self) -> list[Trigger]:
        now_ms = int(time.time() * 1000)
        lookback_ms = int(self.settings.vlm_agent_loitering_dwell_seconds * 2 * 1000)
        window_start_ms = now_ms - lookback_ms

        pool = database.get_pool()
        async with pool.acquire() as connection:
            loitering_rows = await connection.fetch(
                _LOITERING_CANDIDATES_SQL,
                window_start_ms,
                int(self.settings.vlm_agent_loitering_dwell_seconds * 1000),
            )
            crowd_rows = await connection.fetch(
                _CROWD_DENSITY_CANDIDATES_SQL,
                window_start_ms,
                self.settings.vlm_agent_crowd_density_min_targets,
            )

        triggers: list[Trigger] = []
        seen_cameras: set[str] = set()

        # One trigger per camera per scan: a camera loitering AND crowded at
        # once still only affords one reasoning call, and the reasoning
        # prompt is free to describe both in its rationale regardless of
        # which hint fired first.
        for row in loitering_rows:
            camera_id = str(row["camera_id"])
            if camera_id in seen_cameras:
                continue
            if self._ready(camera_id, "loitering"):
                triggers.append(Trigger(camera_id=camera_id, hint="loitering"))
                seen_cameras.add(camera_id)

        for row in crowd_rows:
            camera_id = str(row["camera_id"])
            if camera_id in seen_cameras:
                continue
            if self._ready(camera_id, "crowd density"):
                triggers.append(Trigger(camera_id=camera_id, hint="crowd density"))
                seen_cameras.add(camera_id)

        return triggers

    def _ready(self, camera_id: str, hint: str) -> bool:
        key = (camera_id, hint)
        now = time.monotonic()
        last = self._last_triggered.get(key)
        if last is not None and now - last < self.settings.vlm_agent_trigger_cooldown_seconds:
            return False
        self._last_triggered[key] = now
        return True


async def _capture_clip_frames(
    stream_url: str, settings: Settings
) -> list[np.ndarray]:
    """Pull a short clip from `stream_url`, returning a handful of sampled frames.

    Same rawvideo-over-a-pipe shape as services/anpr/service.py's frame
    grabber, sized down for a VLM's input rather than a plate detector's.
    Returns whatever frames arrived before the clip duration or a read
    timeout, rather than raising - a short or empty clip is reported by the
    caller as a failed reasoning attempt, not a crash.
    """
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", stream_url,
        "-an",
        "-vf", f"fps={_CLIP_SAMPLE_FPS}",
        "-s", f"{_CLIP_FRAME_WIDTH}x{_CLIP_FRAME_HEIGHT}",
        "-t", str(settings.vlm_agent_clip_seconds),
        "-f", "rawvideo",
        "-pix_fmt", "bgr24",
        "pipe:1",
    ]
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    frame_bytes = _CLIP_FRAME_WIDTH * _CLIP_FRAME_HEIGHT * 3
    frames: list[np.ndarray] = []
    deadline = time.monotonic() + settings.vlm_agent_clip_seconds + 10.0

    try:
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(
                    process.stdout.readexactly(frame_bytes), timeout=10.0
                )
            except (asyncio.IncompleteReadError, asyncio.TimeoutError):
                break
            frames.append(
                np.frombuffer(raw, dtype=np.uint8)
                .reshape((_CLIP_FRAME_HEIGHT, _CLIP_FRAME_WIDTH, 3))
                .copy()
            )
    finally:
        if process.returncode is None:
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()

    return frames


async def process_trigger(
    trigger: Trigger,
    settings: Settings,
    vlm_client: VLMClient,
    on_finding: Callable[
        [Trigger, SceneFinding, tuple[int, int], "tuple[float, float] | None"], None
    ],
) -> bool:
    """Capture a clip for one trigger, reason over it, and hand off any finding.

    Returns True when a finding was produced (whether or not `on_finding`
    ultimately published it), so the caller can count attempts vs. findings.
    `on_finding` is a plain callback rather than this function owning the
    Kafka producer, so it stays testable against a stub without a broker.
    """
    pool = database.get_pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(_CAMERA_STREAM_SQL, uuid.UUID(trigger.camera_id))

    if row is None or not row["stream_url"]:
        logger.warning(
            "trigger camera has no active stream; skipping",
            extra={"camera_id": trigger.camera_id, "hint": trigger.hint},
        )
        return False

    stream_url = _with_grid_credentials(row["stream_url"], settings)
    window_start_ms = int(time.time() * 1000)
    frames = await _capture_clip_frames(stream_url, settings)
    window_end_ms = int(time.time() * 1000)

    if not frames:
        logger.warning(
            "no frames captured for trigger; skipping reasoning call",
            extra={"camera_id": trigger.camera_id, "hint": trigger.hint},
        )
        return False

    finding = await asyncio.to_thread(
        vlm_client.reason_about_clip,
        frames,
        hint=trigger.hint,
        duration_seconds=settings.vlm_agent_clip_seconds,
    )
    if finding is None:
        return False

    latitude = float(row["latitude"]) if row["latitude"] is not None else None
    longitude = float(row["longitude"]) if row["longitude"] is not None else None
    position = (latitude, longitude) if latitude is not None and longitude is not None else None

    on_finding(trigger, finding, (window_start_ms, window_end_ms), position)
    return True
