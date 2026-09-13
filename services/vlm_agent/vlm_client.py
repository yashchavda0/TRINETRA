"""Local VLM inference for services/vlm_agent, plus a pluggable hosted fallback.

Everything here is pure model-call work: given an image or a short list of
sampled frames, produce a narrowly-scoped structured result. It has no
knowledge of Kafka, cameras or the analytics bus, so it can be tested against
a still image without any of the surrounding infrastructure - the same
separation reader.py keeps from service.py in services/anpr.

Two entry points:

* `tag_vehicle(image)` -> `VehicleTags | None`     (Tier A, cheap, per-detection)
* `reason_about_clip(frames, hint)` -> `SceneFinding | None`  (Tier B, gated)

Both return None on any failure - model error, a timeout, output that will not
parse as the expected JSON shape - rather than raising. A caller counts the
failure and moves on; a guess must never be published as fact, the same
posture services/anpr/reader.py's `format_valid` gate takes with OCR output.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Final

import numpy as np

logger: Final = logging.getLogger("services.vlm_agent.vlm_client")

_JSON_BLOCK_RE: Final = re.compile(r"\{.*\}", re.DOTALL)

_TAG_VEHICLE_PROMPT: Final = (
    "You are looking at one cropped photo of a single vehicle from a traffic "
    "camera. Reply with ONLY a JSON object, no other text, with exactly these "
    "keys: \"color\" (one lowercase word, e.g. \"white\"), \"make\" (lowercase "
    "manufacturer, or \"unknown\"), \"model\" (lowercase, or \"unknown\"), "
    "\"vehicle_type\" (one of: sedan, suv, hatchback, pickup, van, bus, truck, "
    "motorcycle, auto-rickshaw, other)."
)

_REASON_ABOUT_CLIP_PROMPT_TEMPLATE: Final = (
    "You are reviewing {frame_count} frames sampled evenly across a {duration:.0f}"
    "-second clip from a fixed traffic/surveillance camera. A cheap trigger "
    "flagged this window as a possible \"{hint}\" candidate. Look at the frames "
    "and decide what is actually happening. Reply with ONLY a JSON object, no "
    "other text, with exactly these keys: \"event_type\" (one of: LOITERING, "
    "WRONG_WAY, COLLISION, CROWD_DENSITY, UNUSUAL_ACTIVITY, NONE - use NONE if "
    "nothing notable is actually happening), \"confidence\" (a number from 0.0 "
    "to 1.0), \"rationale\" (one or two plain-English sentences explaining what "
    "you saw and why it does or does not support the event_type you chose)."
)

_VALID_VEHICLE_TYPES: Final = frozenset(
    {
        "sedan", "suv", "hatchback", "pickup", "van", "bus", "truck",
        "motorcycle", "auto-rickshaw", "other",
    }
)
_VALID_SCENE_EVENT_TYPES: Final = frozenset(
    {"LOITERING", "WRONG_WAY", "COLLISION", "CROWD_DENSITY", "UNUSUAL_ACTIVITY"}
)


@dataclass(frozen=True, slots=True)
class VehicleTags:
    color: str
    make: str
    model: str
    vehicle_type: str


@dataclass(frozen=True, slots=True)
class SceneFinding:
    event_type: str
    confidence: float
    rationale: str


def _extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort pull of one JSON object out of a model's raw completion.

    A local small VLM does not reliably honour "reply with ONLY JSON" - it may
    wrap the object in a sentence or a code fence. The regex takes the first
    brace-to-brace span rather than requiring the whole completion to parse,
    because rejecting an otherwise-good answer over a stray "Here is the
    JSON:" prefix would throw away a usable reading.
    """
    match = _JSON_BLOCK_RE.search(text)
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class VLMClient:
    """Loads the local model once at startup and reuses it per call - or, in
    "hosted" mode, opens one long-lived HTTP client instead of loading
    anything in-process.

    Same load-once-reuse posture as `PlateReader.__init__` for the local
    backend: allocating the model is the expensive part, and paying that cost
    per call would make Tier A - which runs once per vehicle detection - far
    too slow to keep up with ANPR's own detection cadence. The hosted backend
    has no load step; the reused resource there is the HTTP connection pool.
    """

    def __init__(
        self,
        model_path: str,
        mmproj_path: str,
        *,
        context_tokens: int = 2048,
        backend: str = "local",
        hosted_api_base_url: str | None = None,
        hosted_api_key: str | None = None,
        hosted_model_name: str = "openai/gpt-oss-20b",
        hosted_request_timeout_seconds: float = 30.0,
    ) -> None:
        self._backend = backend
        self._hosted_model_name = hosted_model_name
        self._hosted_http: Any = None
        self._llm: Any = None
        # Set defensively before the branch below, even though the "hosted"
        # branch returns before reaching the local-only assignment further
        # down - hosted_escalation_available's `and` short-circuits on
        # `_backend` first, but these existing regardless keeps the object
        # introspectable without relying on that ordering.
        self._hosted_base_url = hosted_api_base_url
        self._hosted_api_key = hosted_api_key

        if backend == "hosted":
            if not hosted_api_base_url:
                raise ValueError(
                    "vlm_agent_backend is 'hosted' but vlm_agent_hosted_api_base_url is unset"
                )
            # Imported here, not at module scope, for the same reason the
            # local branch below imports llama_cpp lazily: a caller that only
            # wants the dataclasses or `_extract_json` should not need either
            # dependency installed.
            import httpx

            headers = {}
            if hosted_api_key:
                headers["Authorization"] = f"Bearer {hosted_api_key}"
            self._hosted_http = httpx.Client(
                base_url=hosted_api_base_url.rstrip("/"),
                headers=headers,
                timeout=hosted_request_timeout_seconds,
            )
            logger.info(
                "vlm hosted backend configured",
                extra={"base_url": hosted_api_base_url, "model": hosted_model_name},
            )
            return

        # Imported here, not at module scope, so importing this module (for
        # the dataclasses or `_extract_json`, say) never pays the model-load
        # cost and never requires the dependency to be installed.
        from llama_cpp import Llama
        from llama_cpp.llama_chat_format import Llava15ChatHandler

        started = time.perf_counter()
        self._chat_handler = Llava15ChatHandler(clip_model_path=mmproj_path)
        self._llm = Llama(
            model_path=model_path,
            chat_handler=self._chat_handler,
            n_ctx=context_tokens,
            logits_all=True,
            verbose=False,
        )
        logger.info(
            "vlm loaded",
            extra={
                "model_path": model_path,
                "mmproj_path": mmproj_path,
                "load_seconds": round(time.perf_counter() - started, 2),
            },
        )

    @property
    def hosted_escalation_available(self) -> bool:
        """True when a hosted endpoint is reachable as a secondary path.

        Meaningful only when running the local backend - a client already
        running in "hosted" mode has no separate escalation tier.
        """
        return self._backend == "local" and bool(self._hosted_base_url and self._hosted_api_key)

    # -- Tier A --------------------------------------------------------------

    def tag_vehicle(self, image: np.ndarray) -> VehicleTags | None:
        """One narrow local call: color/make/model/vehicle_type for one crop."""
        completion = self._complete(_TAG_VEHICLE_PROMPT, [image])
        if completion is None:
            return None

        parsed = _extract_json(completion)
        if parsed is None:
            logger.warning("vehicle tagging output did not parse as JSON")
            return None

        vehicle_type = str(parsed.get("vehicle_type", "")).strip().lower()
        if vehicle_type not in _VALID_VEHICLE_TYPES:
            vehicle_type = "other"

        try:
            return VehicleTags(
                color=str(parsed.get("color", "")).strip().lower()[:32] or "unknown",
                make=str(parsed.get("make", "")).strip().lower()[:32] or "unknown",
                model=str(parsed.get("model", "")).strip().lower()[:32] or "unknown",
                vehicle_type=vehicle_type,
            )
        except (TypeError, ValueError):
            logger.warning("vehicle tagging output had the wrong shape")
            return None

    # -- Tier B --------------------------------------------------------------

    def reason_about_clip(
        self, frames: list[np.ndarray], *, hint: str, duration_seconds: float
    ) -> SceneFinding | None:
        """One higher-cost local call over several sampled frames from a clip.

        `hint` is the cheap trigger's guess (e.g. "loitering"); the model is
        asked to confirm, reclassify or dismiss it rather than treating the
        hint as already-established fact - a dwell-time trigger firing is
        evidence a human should look, not evidence of loitering itself.
        """
        prompt = _REASON_ABOUT_CLIP_PROMPT_TEMPLATE.format(
            frame_count=len(frames), duration=duration_seconds, hint=hint
        )
        completion = self._complete(prompt, frames)
        if completion is None:
            return None

        parsed = _extract_json(completion)
        if parsed is None:
            logger.warning("scene reasoning output did not parse as JSON")
            return None

        event_type = str(parsed.get("event_type", "")).strip().upper()
        if event_type in {"", "NONE"}:
            return None
        if event_type not in _VALID_SCENE_EVENT_TYPES:
            logger.warning("scene reasoning returned an unknown event_type", extra={
                "event_type": event_type,
            })
            return None

        try:
            confidence = float(parsed.get("confidence", 0.0))
        except (TypeError, ValueError):
            return None
        confidence = max(0.0, min(1.0, confidence))

        rationale = str(parsed.get("rationale", "")).strip()
        if not rationale:
            return None

        return SceneFinding(event_type=event_type, confidence=confidence, rationale=rationale[:2000])

    # -- shared call path ------------------------------------------------

    def _complete(self, prompt: str, images: list[np.ndarray]) -> str | None:
        """Run one chat completion over `prompt` plus `images`. None on any failure.

        Same request shape either way - a list of content parts, one text and
        one image_url per frame - because both the local llama.cpp vision
        chat handler and an OpenAI-compatible /v1/chat/completions endpoint
        accept it. Only how the completion is obtained differs.
        """
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image in images:
            content.append({"type": "image_url", "image_url": {"url": _to_data_url(image)}})
        messages = [{"role": "user", "content": content}]

        if self._backend == "hosted":
            return self._complete_hosted(messages)
        return self._complete_local(messages)

    def _complete_local(self, messages: list[dict[str, Any]]) -> str | None:
        try:
            response = self._llm.create_chat_completion(
                messages=messages, temperature=0.1, max_tokens=300
            )
            return response["choices"][0]["message"]["content"]
        except Exception:
            logger.exception("local vlm inference failed")
            return None

    def _complete_hosted(self, messages: list[dict[str, Any]]) -> str | None:
        try:
            response = self._hosted_http.post(
                "/chat/completions",
                json={
                    "model": self._hosted_model_name,
                    "messages": messages,
                    "temperature": 0.1,
                    "max_tokens": 300,
                },
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except Exception:
            logger.exception("hosted vlm inference failed")
            return None


def _to_data_url(image: np.ndarray) -> str:
    """Encode a BGR ndarray as a base64 data: URL, the shape llama.cpp's vision
    chat handlers expect for an image_url content part."""
    import base64

    import cv2

    ok, buffer = cv2.imencode(".jpg", image)
    if not ok:
        raise ValueError("failed to JPEG-encode frame for VLM inference")
    encoded = base64.b64encode(buffer.tobytes()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"
