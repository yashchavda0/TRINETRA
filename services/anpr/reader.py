"""Plate detection, OCR, motion gating and format-aware correction.

Everything here is pure CPU work on a frame. It has no knowledge of cameras,
scheduling or Kafka, so it can be benchmarked and tested against a still image
without any of the surrounding infrastructure.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Final

import cv2
import numpy as np

logger: Final = logging.getLogger("services.anpr.reader")

# Indian registration marks, separators stripped and upper-cased. Same pattern
# the adapters use, repeated here so this module stays importable without the
# application package - it is the format the correction pass validates against.
_PLATE_RE: Final = re.compile(
    r"^(?:[A-Z]{2}\d{1,2}[A-Z]{0,3}\d{1,4}|\d{2}BH\d{4}[A-Z]{1,2})$"
)
_STRIP_RE: Final = re.compile(r"[^A-Z0-9]")

# OCR confusions, in both directions. A plate font renders 0 and O nearly
# identically at 60 pixels wide, and the reader has no way to know which the
# format demands - but the format does, so the correction is driven from it.
_TO_DIGIT: Final = {
    "O": "0", "Q": "0", "D": "0",
    "I": "1", "L": "1",
    "Z": "2",
    "S": "5",
    "G": "6",
    "T": "7",
    "B": "8",
}
_TO_LETTER: Final = {
    "0": "O",
    "1": "I",
    "2": "Z",
    "4": "A",
    "5": "S",
    "6": "G",
    "8": "B",
}


@dataclass(frozen=True, slots=True)
class PlateReading:
    """One plate read from one frame."""

    plate: str
    confidence: float
    #: Pixel box in the source frame: (x1, y1, x2, y2).
    box: tuple[int, int, int, int]
    #: True when format-aware correction changed the OCR's raw output.
    corrected: bool
    raw: str
    #: True only when `plate` actually matched the Indian registration format.
    #: False means the detector fired on something - a sign, a motion-blur
    #: streak, anything rectangular enough to look like a plate - and the OCR
    #: transcribed whatever text was there. `corrected` says a substitution was
    #: tried; `format_valid` says the result is actually plate-shaped. A caller
    #: publishing plate readings as fact must gate on this, not on `corrected`.
    format_valid: bool


def _candidates(text: str) -> list[str]:
    """Plausible readings of `text`, best first.

    The raw reading is always tried first: correction is a fallback for a
    string the format rejects, never an override of one it accepts.
    """
    cleaned = _STRIP_RE.sub("", text.upper())
    if not cleaned:
        return []

    options = [cleaned]

    # Standard format is LL DD L{0,3} DDDD: the first two characters are always
    # letters and the next one or two always digits. Those four positions carry
    # most of the confusion, and they are unambiguous in the format.
    if len(cleaned) >= 4:
        chars = list(cleaned)
        for index in (0, 1):
            chars[index] = _TO_LETTER.get(chars[index], chars[index])
        for index in (2, 3):
            chars[index] = _TO_DIGIT.get(chars[index], chars[index])
        options.append("".join(chars))

        # The tail of a standard mark is the numeric series.
        tail = list(chars)
        for index in range(len(tail) - 1, max(len(tail) - 5, 3), -1):
            tail[index] = _TO_DIGIT.get(tail[index], tail[index])
        options.append("".join(tail))

    seen: set[str] = set()
    return [o for o in options if not (o in seen or seen.add(o))]


def correct_plate(text: str) -> tuple[str, bool, bool]:
    """Return (best_reading, was_corrected, format_valid).

    Tries the raw reading against the Indian plate format, then format-aware
    character substitutions. When nothing validates, the cleaned raw text is
    returned unchanged with `format_valid=False` - a misread is still evidence
    of what the camera saw, and inventing a valid-looking plate would be far
    worse than recording an unmatchable one. `format_valid` is what lets a
    caller tell that case apart from an actual plate-shaped reading: it must
    not be inferred from `was_corrected`, which is true or false independently
    of whether the result matches the format at all.
    """
    options = _candidates(text)
    if not options:
        return "", False, False

    for index, candidate in enumerate(options):
        if _PLATE_RE.match(candidate):
            return candidate, index > 0, True

    return options[0], False, False


def _letterbox(crop: np.ndarray, target_width: int, target_height: int) -> np.ndarray:
    """Resize `crop` to exactly (target_width, target_height) without distorting it.

    A plain `cv2.resize` stretches whatever aspect ratio the detector's box
    happened to have into the model's fixed input - the smaller and more
    oblong the source crop (a distant or angled plate), the more the character
    proportions warp, which costs real OCR accuracy on exactly the reads that
    are already hardest. This scales to fit, then pads with black to the
    target size, so a character keeps its true shape regardless of the box.
    """
    height, width = crop.shape[:2]
    scale = min(target_width / width, target_height / height)
    new_width = max(1, round(width * scale))
    new_height = max(1, round(height * scale))

    scaled = cv2.resize(crop, (new_width, new_height), interpolation=cv2.INTER_CUBIC)

    canvas = np.zeros((target_height, target_width, 3), dtype=crop.dtype)
    x_offset = (target_width - new_width) // 2
    y_offset = (target_height - new_height) // 2
    canvas[y_offset : y_offset + new_height, x_offset : x_offset + new_width] = scaled
    return canvas


class PlateReader:
    """Detector + OCR + motion gate, loaded once and reused per frame."""

    def __init__(
        self,
        detector_model: str,
        ocr_model: str,
        *,
        min_detection_confidence: float = 0.4,
        motion_threshold: float = 0.002,
        motion_pixel_delta: int = 25,
    ) -> None:
        # Imported here rather than at module scope so that importing this
        # module (for `correct_plate`, say) does not pay the ONNX load cost.
        from fast_plate_ocr import LicensePlateRecognizer
        from open_image_models import create_detector

        started = time.perf_counter()
        self._detector = create_detector(
            detector_model, providers=["CPUExecutionProvider"]
        )
        self._ocr = LicensePlateRecognizer(hub_ocr_model=ocr_model, device="cpu")
        logger.info(
            "plate models loaded",
            extra={
                "detector": detector_model,
                "ocr": ocr_model,
                "load_seconds": round(time.perf_counter() - started, 2),
            },
        )

        self.min_detection_confidence = min_detection_confidence
        self.motion_threshold = motion_threshold
        self.motion_pixel_delta = motion_pixel_delta

        # Previous downscaled greyscale frame, per camera.
        self._previous: dict[str, np.ndarray] = {}

    # -- motion gating -----------------------------------------------------

    def has_motion(self, camera_id: str, frame: np.ndarray) -> bool:
        """True when enough pixels changed since this camera's last frame.

        The first frame from a camera always passes: with nothing to compare
        against, skipping it would mean never looking at a camera whose scene
        happens to be static at start-up.
        """
        small = cv2.cvtColor(cv2.resize(frame, (320, 180)), cv2.COLOR_BGR2GRAY)
        previous = self._previous.get(camera_id)
        self._previous[camera_id] = small

        if previous is None:
            return True

        diff = cv2.absdiff(small, previous)
        changed = float(np.count_nonzero(diff > self.motion_pixel_delta)) / diff.size
        return changed >= self.motion_threshold

    def forget(self, camera_id: str) -> None:
        """Drop a camera's motion reference, e.g. when it is decommissioned."""
        self._previous.pop(camera_id, None)

    # -- reading -----------------------------------------------------------

    def read(self, frame: np.ndarray) -> list[PlateReading]:
        """Detect plates in a frame and OCR each one."""
        readings: list[PlateReading] = []

        for detection in self._detector.predict(frame):
            confidence = float(detection.confidence)
            if confidence < self.min_detection_confidence:
                continue

            box = detection.bounding_box
            height, width = frame.shape[:2]
            # Clamp: a box may extend past the frame edge on a plate that is
            # partly out of shot, and a negative slice silently returns the
            # wrong region rather than failing.
            x1 = max(0, min(int(box.x1), width - 1))
            y1 = max(0, min(int(box.y1), height - 1))
            x2 = max(x1 + 1, min(int(box.x2), width))
            y2 = max(y1 + 1, min(int(box.y2), height))

            crop = frame[y1:y2, x1:x2]
            if crop.size == 0:
                continue

            # The OCR model has a fixed input geometry; anything else raises
            # inside onnxruntime rather than being resized for us.
            resized = _letterbox(crop, 128, 64)

            try:
                predictions = self._ocr.run(resized)
            except Exception:
                logger.exception("OCR failed on a plate crop")
                continue

            if not predictions:
                continue

            raw = str(getattr(predictions[0], "plate", "") or "")
            plate, corrected, format_valid = correct_plate(raw)
            if not plate:
                continue

            readings.append(
                PlateReading(
                    plate=plate,
                    confidence=confidence,
                    box=(x1, y1, x2, y2),
                    corrected=corrected,
                    raw=raw,
                    format_valid=format_valid,
                )
            )

        return readings
