"""Model 4 dynamic wake-up and CUDA-accelerated re-identification matcher.

Holds the active target embeddings on GPU, scores candidate detections from a
downstream camera by cosine similarity, and drives the wake-up state machine

    PASSIVE -> PRE_ACTIVATED -> ACTIVE_TRACKING -> COOLDOWN -> PASSIVE
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

import torch
from torch import Tensor

logger: Final = logging.getLogger(__name__)

EMBEDDING_DIM: Final[int] = 512
SIMILARITY_THRESHOLD: Final[float] = 0.85
HANDOFF_PROBABILITY_THRESHOLD: Final[float] = 0.65
COOLDOWN_SECONDS: Final[float] = 60.0

# Guard against divide-by-zero on all-zero embeddings.
_NORM_EPS: Final[float] = 1e-12


def resolve_device(device: torch.device | str | None = None) -> torch.device:
    """Prefer CUDA; fall back to CPU so the engine still runs off-GPU."""
    if device is not None:
        return torch.device(device)
    if torch.cuda.is_available():
        return torch.device("cuda")
    logger.warning("CUDA unavailable; vector matching will run on CPU")
    return torch.device("cpu")


class ActiveTargetsCache:
    """In-memory target_id -> tensor(512, float32) cache resident on device."""

    def __init__(
        self,
        *,
        device: torch.device | str | None = None,
        max_targets: int = 4096,
    ) -> None:
        if max_targets < 1:
            raise ValueError("max_targets must be at least 1")

        self.device = resolve_device(device)
        self.max_targets = int(max_targets)
        self._vectors: dict[str, Tensor] = {}
        self._last_seen: dict[str, float] = {}
        self._lock = threading.RLock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._vectors)

    def __contains__(self, target_id: str) -> bool:
        with self._lock:
            return target_id in self._vectors

    def upsert(self, target_id: str, embedding: Tensor) -> None:
        """Store or refresh a target vector, normalising shape and dtype."""
        vector = self._validate(embedding)

        with self._lock:
            if target_id not in self._vectors and len(self._vectors) >= self.max_targets:
                self._evict_oldest_locked()
            self._vectors[target_id] = vector
            self._last_seen[target_id] = time.monotonic()

    def get(self, target_id: str) -> Tensor:
        """Return the target vector shaped 1 x 512."""
        with self._lock:
            vector = self._vectors.get(target_id)
            if vector is None:
                raise KeyError(f"target_id not in active cache: {target_id}")
            self._last_seen[target_id] = time.monotonic()
            return vector

    def remove(self, target_id: str) -> bool:
        with self._lock:
            existed = self._vectors.pop(target_id, None) is not None
            self._last_seen.pop(target_id, None)
        return existed

    def target_ids(self) -> list[str]:
        with self._lock:
            return list(self._vectors)

    def purge_expired(self, max_age_seconds: float) -> list[str]:
        """Drop targets untouched for longer than ``max_age_seconds``."""
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")

        now = time.monotonic()
        with self._lock:
            expired = [
                target_id
                for target_id, seen in self._last_seen.items()
                if now - seen > max_age_seconds
            ]
            for target_id in expired:
                self._vectors.pop(target_id, None)
                self._last_seen.pop(target_id, None)

        if expired:
            logger.info("purged %d expired target(s) from active cache", len(expired))
        return expired

    def _evict_oldest_locked(self) -> None:
        oldest = min(self._last_seen, key=self._last_seen.__getitem__, default=None)
        if oldest is not None:
            self._vectors.pop(oldest, None)
            self._last_seen.pop(oldest, None)
            logger.warning("active target cache full; evicted target %s", oldest)

    def _validate(self, embedding: Tensor) -> Tensor:
        if not isinstance(embedding, Tensor):
            raise TypeError("embedding must be a torch.Tensor")

        vector = embedding.detach()
        if vector.dim() == 1:
            vector = vector.unsqueeze(0)
        if vector.dim() != 2 or vector.shape[0] != 1 or vector.shape[1] != EMBEDDING_DIM:
            raise ValueError(
                f"embedding must be shaped ({EMBEDDING_DIM},) or (1, {EMBEDDING_DIM}); "
                f"got {tuple(embedding.shape)}"
            )
        return vector.to(device=self.device, dtype=torch.float32).contiguous()


class VectorMatcher:
    """Cosine-similarity re-identification scorer running on the GPU."""

    def __init__(
        self,
        cache: ActiveTargetsCache | None = None,
        *,
        device: torch.device | str | None = None,
        similarity_threshold: float = SIMILARITY_THRESHOLD,
    ) -> None:
        if not -1.0 <= similarity_threshold <= 1.0:
            raise ValueError("similarity_threshold must be within [-1.0, 1.0]")

        self.device = resolve_device(device)
        self.cache = cache if cache is not None else ActiveTargetsCache(device=self.device)
        self.similarity_threshold = float(similarity_threshold)

    @torch.no_grad()
    def evaluate_candidate_frame(
        self, candidate_embeddings: Tensor, target_id: str
    ) -> list[int]:
        """Return indices of candidates matching the target above threshold."""
        candidates = self._validate_candidates(candidate_embeddings)
        if candidates.shape[0] == 0:
            return []

        target_vector = self.cache.get(target_id).to(self.device, torch.float32)

        similarity = torch.mm(candidates, target_vector.T) / (
            torch.norm(candidates, dim=1, keepdim=True).clamp_min(_NORM_EPS)
            * torch.norm(target_vector).clamp_min(_NORM_EPS)
        )
        similarity = similarity.squeeze(1)

        matches = torch.nonzero(similarity >= self.similarity_threshold, as_tuple=False)
        return matches.squeeze(1).tolist()

    @torch.no_grad()
    def best_match(
        self, candidate_embeddings: Tensor, target_id: str
    ) -> tuple[int, float] | None:
        """Highest-scoring candidate above threshold, or None."""
        candidates = self._validate_candidates(candidate_embeddings)
        if candidates.shape[0] == 0:
            return None

        target_vector = self.cache.get(target_id).to(self.device, torch.float32)
        similarity = torch.mm(candidates, target_vector.T) / (
            torch.norm(candidates, dim=1, keepdim=True).clamp_min(_NORM_EPS)
            * torch.norm(target_vector).clamp_min(_NORM_EPS)
        )
        similarity = similarity.squeeze(1)

        score, index = torch.max(similarity, dim=0)
        if float(score) < self.similarity_threshold:
            return None
        return int(index), float(score)

    def _validate_candidates(self, candidate_embeddings: Tensor) -> Tensor:
        if not isinstance(candidate_embeddings, Tensor):
            raise TypeError("candidate_embeddings must be a torch.Tensor")

        candidates = candidate_embeddings.detach()
        if candidates.dim() == 1:
            candidates = candidates.unsqueeze(0)
        if candidates.dim() != 2 or candidates.shape[1] != EMBEDDING_DIM:
            raise ValueError(
                f"candidate_embeddings must be shaped N x {EMBEDDING_DIM}; "
                f"got {tuple(candidate_embeddings.shape)}"
            )
        return candidates.to(device=self.device, dtype=torch.float32).contiguous()


class CameraState(str, Enum):
    PASSIVE = "PASSIVE"
    PRE_ACTIVATED = "PRE_ACTIVATED"
    ACTIVE_TRACKING = "ACTIVE_TRACKING"
    COOLDOWN = "COOLDOWN"


@dataclass
class CameraStateRecord:
    """Wake-up state for one candidate camera."""

    camera_id: str
    state: CameraState = CameraState.PASSIVE
    target_id: str | None = None
    handoff_probability: float = 0.0
    entered_at: float = field(default_factory=time.monotonic)
    cooldown_until: float | None = None


class CameraStateMachine:
    """Wake-up controller for downstream candidate cameras.

    PASSIVE -> (STGCN probability >= 0.65) -> PRE_ACTIVATED
    PRE_ACTIVATED -> (target matched) -> ACTIVE_TRACKING
    ACTIVE_TRACKING -> (target exits FOV) -> COOLDOWN (60s) -> PASSIVE
    """

    def __init__(
        self,
        *,
        probability_threshold: float = HANDOFF_PROBABILITY_THRESHOLD,
        cooldown_seconds: float = COOLDOWN_SECONDS,
        pre_activation_timeout_seconds: float = 30.0,
    ) -> None:
        if not 0.0 < probability_threshold <= 1.0:
            raise ValueError("probability_threshold must be in (0, 1]")
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be positive")
        if pre_activation_timeout_seconds <= 0:
            raise ValueError("pre_activation_timeout_seconds must be positive")

        self.probability_threshold = float(probability_threshold)
        self.cooldown_seconds = float(cooldown_seconds)
        self.pre_activation_timeout_seconds = float(pre_activation_timeout_seconds)

        self._records: dict[str, CameraStateRecord] = {}
        self._lock = threading.RLock()

    def state_of(self, camera_id: str) -> CameraState:
        with self._lock:
            record = self._records.get(camera_id)
            return record.state if record else CameraState.PASSIVE

    def snapshot(self) -> dict[str, CameraStateRecord]:
        with self._lock:
            return dict(self._records)

    def cameras_in_state(self, state: CameraState) -> list[str]:
        with self._lock:
            return [cid for cid, rec in self._records.items() if rec.state is state]

    def signal_from_stgcn(
        self, camera_id: str, target_id: str, probability: float
    ) -> CameraState:
        """PASSIVE -> PRE_ACTIVATED when the handoff prior clears threshold."""
        if probability < self.probability_threshold:
            return self.state_of(camera_id)

        with self._lock:
            record = self._records.setdefault(camera_id, CameraStateRecord(camera_id))

            if record.state is CameraState.COOLDOWN:
                if record.cooldown_until is not None and time.monotonic() < record.cooldown_until:
                    return record.state
                self._transition_locked(record, CameraState.PASSIVE)

            if record.state is CameraState.PASSIVE:
                record.target_id = target_id
                record.handoff_probability = float(probability)
                self._transition_locked(record, CameraState.PRE_ACTIVATED)
            elif record.state is CameraState.PRE_ACTIVATED:
                record.handoff_probability = max(
                    record.handoff_probability, float(probability)
                )

            return record.state

    def confirm_match(self, camera_id: str, target_id: str) -> CameraState:
        """PRE_ACTIVATED -> ACTIVE_TRACKING once the target is re-identified."""
        with self._lock:
            record = self._records.get(camera_id)
            if record is None or record.state is not CameraState.PRE_ACTIVATED:
                return self.state_of(camera_id)

            record.target_id = target_id
            self._transition_locked(record, CameraState.ACTIVE_TRACKING)
            return record.state

    def target_exited_fov(self, camera_id: str) -> CameraState:
        """ACTIVE_TRACKING -> COOLDOWN for the configured window."""
        with self._lock:
            record = self._records.get(camera_id)
            if record is None or record.state is not CameraState.ACTIVE_TRACKING:
                return self.state_of(camera_id)

            record.cooldown_until = time.monotonic() + self.cooldown_seconds
            record.target_id = None
            self._transition_locked(record, CameraState.COOLDOWN)
            return record.state

    def tick(self) -> list[str]:
        """Expire cooldowns and stale pre-activations; returns reset cameras."""
        now = time.monotonic()
        reset: list[str] = []

        with self._lock:
            for record in self._records.values():
                if (
                    record.state is CameraState.COOLDOWN
                    and record.cooldown_until is not None
                    and now >= record.cooldown_until
                ):
                    record.cooldown_until = None
                    self._transition_locked(record, CameraState.PASSIVE)
                    reset.append(record.camera_id)
                elif (
                    record.state is CameraState.PRE_ACTIVATED
                    and now - record.entered_at > self.pre_activation_timeout_seconds
                ):
                    # Predicted handoff never materialised; release the wake-up.
                    record.target_id = None
                    record.handoff_probability = 0.0
                    self._transition_locked(record, CameraState.PASSIVE)
                    reset.append(record.camera_id)

        return reset

    def _transition_locked(
        self, record: CameraStateRecord, new_state: CameraState
    ) -> None:
        if record.state is new_state:
            return
        logger.info(
            "camera %s state %s -> %s (target=%s)",
            record.camera_id,
            record.state.value,
            new_state.value,
            record.target_id,
        )
        record.state = new_state
        record.entered_at = time.monotonic()
