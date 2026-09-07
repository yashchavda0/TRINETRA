"""Model 4 Real-Time Predictive Spatial-Temporal Camera Handoff Engine."""

from engine.tracking_pipeline import (
    CameraNode,
    PredictiveHandoffManager,
    SpatialCameraGraph,
    STGCNPredictor,
)
from engine.vector_matcher import (
    ActiveTargetsCache,
    CameraState,
    CameraStateMachine,
    VectorMatcher,
)

__all__ = [
    "CameraNode",
    "SpatialCameraGraph",
    "STGCNPredictor",
    "PredictiveHandoffManager",
    "ActiveTargetsCache",
    "CameraState",
    "CameraStateMachine",
    "VectorMatcher",
]
