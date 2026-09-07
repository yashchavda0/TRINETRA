"""Model 4 STGCN predictive tracking pipeline.

Builds the camera graph G = (V, E, W) from the Model 1 PostGIS registry and
scores downstream handoff candidates with

    P(C_j | C_i, v, theta, dt)
        = sigma(f_STGCN(G, H^k))
          * exp(-((dt - d_ij / ||v||)^2) / (2 * sigma_t^2))
          * ((1 + cos(theta, u_ij)) / 2)

Spatial conventions follow the registry: WGS84 / EPSG:4326, longitude is X,
latitude is Y, and every bearing is a compass bearing in degrees where 0 is
true north and values increase clockwise.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Final, Iterable, Sequence

import asyncpg
import torch
import torch.nn.functional as F
from torch import Tensor, nn
from torch_geometric.nn import GCNConv

logger: Final = logging.getLogger(__name__)

# Road-network proximity ceiling for an edge, in metres.
DEFAULT_EDGE_DISTANCE_THRESHOLD_M: Final[float] = 1_500.0

# Fleet-wide fallback ground speed used when no historical transit time exists.
DEFAULT_TRANSIT_SPEED_MPS: Final[float] = 11.1  # ~40 km/h urban arterial

# Minimum speed treated as "moving"; below this the kinematic term is undefined.
MIN_VELOCITY_MPS: Final[float] = 0.5

DEFAULT_PROBABILITY_THRESHOLD: Final[float] = 0.65

_CAMERA_NODE_QUERY: Final = """
    SELECT
        id::text                    AS camera_id,
        global_camera_code,
        department_id,
        ST_X(location_geom)         AS longitude,
        ST_Y(location_geom)         AS latitude,
        COALESCE(azimuth_angle, 0)::double precision AS azimuth_degrees
    FROM cameras
    WHERE status = 'ACTIVE'
    ORDER BY id
"""

# Edges are built inside PostGIS: an 80,000-node fleet makes an in-process
# O(n^2) pairwise scan (6.4e9 pairs) untenable, while the GIST geography index
# turns this into a bounded radius join.
_CAMERA_EDGE_QUERY: Final = """
    SELECT
        src.id::text AS source_id,
        dst.id::text AS target_id,
        ST_Distance(src.location_geom::geography, dst.location_geom::geography) AS distance_m,
        DEGREES(ST_Azimuth(src.location_geom, dst.location_geom))               AS bearing_degrees
    FROM cameras AS src
    JOIN cameras AS dst
      ON dst.id <> src.id
     AND ST_DWithin(src.location_geom::geography, dst.location_geom::geography, $1)
    WHERE src.status = 'ACTIVE'
      AND dst.status = 'ACTIVE'
"""

# Optional observed-transit table. Absent in a fresh deployment, in which case
# tau_ij falls back to a distance/speed estimate.
_TRANSIT_TIME_QUERY: Final = """
    SELECT
        source_camera_id::text AS source_id,
        target_camera_id::text AS target_id,
        median_transit_seconds::double precision AS tau_seconds
    FROM camera_transition_stats
"""


@dataclass(frozen=True, slots=True)
class CameraNode:
    """One vertex of G: a registry camera with its WGS84 mount position."""

    camera_id: str
    global_camera_code: str
    department_id: str | None
    longitude: float
    latitude: float
    azimuth_degrees: float


@dataclass(frozen=True, slots=True)
class CameraEdge:
    """One directed edge of G carrying W = (d_ij, tau_ij, bearing)."""

    source_id: str
    target_id: str
    distance_m: float
    transit_seconds: float
    bearing_degrees: float


class SpatialCameraGraph:
    """Camera graph G = (V, E, W) loaded from the PostGIS registry."""

    def __init__(
        self,
        dsn: str,
        *,
        distance_threshold_m: float = DEFAULT_EDGE_DISTANCE_THRESHOLD_M,
        device: torch.device | str = "cpu",
    ) -> None:
        if distance_threshold_m <= 0:
            raise ValueError("distance_threshold_m must be positive")

        self._dsn = dsn
        self.distance_threshold_m = float(distance_threshold_m)
        self.device = torch.device(device)

        self.nodes: list[CameraNode] = []
        self.edges: list[CameraEdge] = []
        self.node_index: dict[str, int] = {}

        self.edge_index: Tensor = torch.empty((2, 0), dtype=torch.long, device=self.device)
        # Columns: d_ij (metres), tau_ij (seconds), bearing (degrees).
        self.edge_attr: Tensor = torch.empty((0, 3), dtype=torch.float32, device=self.device)
        self.edge_weight: Tensor = torch.empty((0,), dtype=torch.float32, device=self.device)
        self.node_features: Tensor = torch.empty((0, 0), dtype=torch.float32, device=self.device)

        # Adjacency in insertion order, so candidate lookup is O(degree).
        self._outgoing: dict[int, list[int]] = {}

    @property
    def num_nodes(self) -> int:
        return len(self.nodes)

    @property
    def num_edges(self) -> int:
        return len(self.edges)

    async def load(self) -> None:
        """Populate V, E and W from PostgreSQL/PostGIS."""
        try:
            connection = await asyncpg.connect(dsn=self._dsn)
        except (asyncpg.PostgresError, OSError) as exc:
            raise RuntimeError(f"camera graph load failed: cannot connect: {exc}") from exc

        try:
            node_rows = await connection.fetch(_CAMERA_NODE_QUERY)
            edge_rows = await connection.fetch(
                _CAMERA_EDGE_QUERY, self.distance_threshold_m
            )
            transit_rows = await self._fetch_transit_times(connection)
        except asyncpg.PostgresError as exc:
            raise RuntimeError(f"camera graph load failed: query error: {exc}") from exc
        finally:
            await connection.close()

        self._build_nodes(node_rows)
        self._build_edges(edge_rows, transit_rows)
        self._build_tensors()

        logger.info(
            "camera graph loaded: %d nodes, %d edges (threshold %.0f m)",
            self.num_nodes,
            self.num_edges,
            self.distance_threshold_m,
        )

    def load_blocking(self) -> None:
        """Synchronous entry point for scripts and worker bootstrap."""
        asyncio.run(self.load())

    async def _fetch_transit_times(
        self, connection: asyncpg.Connection
    ) -> dict[tuple[str, str], float]:
        try:
            rows = await connection.fetch(_TRANSIT_TIME_QUERY)
        except asyncpg.UndefinedTableError:
            logger.warning(
                "camera_transition_stats not present; tau_ij falls back to d_ij / %.1f m/s",
                DEFAULT_TRANSIT_SPEED_MPS,
            )
            return {}

        return {
            (row["source_id"], row["target_id"]): float(row["tau_seconds"])
            for row in rows
            if row["tau_seconds"] is not None and float(row["tau_seconds"]) > 0
        }

    def _build_nodes(self, rows: Sequence[asyncpg.Record]) -> None:
        self.nodes = [
            CameraNode(
                camera_id=row["camera_id"],
                global_camera_code=row["global_camera_code"],
                department_id=row["department_id"],
                longitude=float(row["longitude"]),
                latitude=float(row["latitude"]),
                azimuth_degrees=float(row["azimuth_degrees"]),
            )
            for row in rows
        ]
        self.node_index = {node.camera_id: idx for idx, node in enumerate(self.nodes)}

    def _build_edges(
        self,
        rows: Sequence[asyncpg.Record],
        transit_times: dict[tuple[str, str], float],
    ) -> None:
        self.edges = []
        self._outgoing = {}

        for row in rows:
            source_id = row["source_id"]
            target_id = row["target_id"]
            if source_id not in self.node_index or target_id not in self.node_index:
                continue

            distance_m = float(row["distance_m"])
            if distance_m <= 0.0 or distance_m > self.distance_threshold_m:
                continue

            bearing = float(row["bearing_degrees"] or 0.0) % 360.0
            tau = transit_times.get(
                (source_id, target_id), distance_m / DEFAULT_TRANSIT_SPEED_MPS
            )

            edge_position = len(self.edges)
            self.edges.append(
                CameraEdge(
                    source_id=source_id,
                    target_id=target_id,
                    distance_m=distance_m,
                    transit_seconds=tau,
                    bearing_degrees=bearing,
                )
            )
            self._outgoing.setdefault(self.node_index[source_id], []).append(edge_position)

    def _build_tensors(self) -> None:
        if not self.edges:
            self.edge_index = torch.empty((2, 0), dtype=torch.long, device=self.device)
            self.edge_attr = torch.empty((0, 3), dtype=torch.float32, device=self.device)
            self.edge_weight = torch.empty((0,), dtype=torch.float32, device=self.device)
            self.node_features = self._initial_node_features()
            return

        sources = [self.node_index[edge.source_id] for edge in self.edges]
        targets = [self.node_index[edge.target_id] for edge in self.edges]

        self.edge_index = torch.tensor(
            [sources, targets], dtype=torch.long, device=self.device
        )
        self.edge_attr = torch.tensor(
            [
                [edge.distance_m, edge.transit_seconds, edge.bearing_degrees]
                for edge in self.edges
            ],
            dtype=torch.float32,
            device=self.device,
        )
        # Closer cameras propagate more signal during message passing.
        self.edge_weight = torch.clamp(
            1.0 - self.edge_attr[:, 0] / self.distance_threshold_m, min=1e-4
        )
        self.node_features = self._initial_node_features()

    def _initial_node_features(self) -> Tensor:
        """H^(0): normalised position, mount orientation and out-degree."""
        if not self.nodes:
            return torch.empty((0, 5), dtype=torch.float32, device=self.device)

        longitudes = torch.tensor(
            [node.longitude for node in self.nodes], dtype=torch.float32
        )
        latitudes = torch.tensor(
            [node.latitude for node in self.nodes], dtype=torch.float32
        )
        azimuths = torch.tensor(
            [math.radians(node.azimuth_degrees) for node in self.nodes],
            dtype=torch.float32,
        )
        degrees = torch.tensor(
            [float(len(self._outgoing.get(idx, ()))) for idx in range(len(self.nodes))],
            dtype=torch.float32,
        )

        features = torch.stack(
            [
                _standardise(longitudes),
                _standardise(latitudes),
                torch.sin(azimuths),
                torch.cos(azimuths),
                _standardise(degrees),
            ],
            dim=1,
        )
        return features.to(self.device)

    def outgoing_edges(self, camera_id: str) -> list[CameraEdge]:
        """Downstream candidate edges for one camera."""
        node_id = self.node_index.get(camera_id)
        if node_id is None:
            raise KeyError(f"camera_id not present in graph: {camera_id}")
        return [self.edges[pos] for pos in self._outgoing.get(node_id, ())]

    def outgoing_edge_positions(self, camera_id: str) -> list[int]:
        node_id = self.node_index.get(camera_id)
        if node_id is None:
            raise KeyError(f"camera_id not present in graph: {camera_id}")
        return list(self._outgoing.get(node_id, ()))

    def to(self, device: torch.device | str) -> "SpatialCameraGraph":
        self.device = torch.device(device)
        self.edge_index = self.edge_index.to(self.device)
        self.edge_attr = self.edge_attr.to(self.device)
        self.edge_weight = self.edge_weight.to(self.device)
        self.node_features = self.node_features.to(self.device)
        return self


def _standardise(values: Tensor) -> Tensor:
    std = torch.std(values)
    if not torch.isfinite(std) or float(std) < 1e-6:
        return torch.zeros_like(values)
    return (values - torch.mean(values)) / std


class STGCNPredictor(nn.Module):
    """Spatial-temporal GCN scoring f_STGCN(G, H^(k)) for camera pairs."""

    def __init__(
        self,
        in_channels: int = 5,
        hidden_channels: int = 64,
        out_channels: int = 32,
        *,
        num_temporal_layers: int = 1,
        dropout: float = 0.1,
    ) -> None:
        super().__init__()
        if in_channels <= 0 or hidden_channels <= 0 or out_channels <= 0:
            raise ValueError("channel dimensions must be positive")

        self.gcn1 = GCNConv(in_channels, hidden_channels)
        self.gcn2 = GCNConv(hidden_channels, hidden_channels)
        self.gcn3 = GCNConv(hidden_channels, out_channels)
        self.dropout = float(dropout)

        # Temporal aggregation across the H^(k) history window.
        self.temporal = nn.GRU(
            input_size=out_channels,
            hidden_size=out_channels,
            num_layers=num_temporal_layers,
            batch_first=True,
        )

        # Pair scorer: [h_i, h_j, h_i * h_j, d_ij_norm, tau_ij_norm] -> logit.
        self.edge_scorer = nn.Sequential(
            nn.Linear(out_channels * 3 + 2, hidden_channels),
            nn.ReLU(),
            nn.Linear(hidden_channels, 1),
        )

    def encode(
        self,
        node_features: Tensor,
        edge_index: Tensor,
        edge_weight: Tensor | None = None,
    ) -> Tensor:
        """Run spatial message passing for a single time slice."""
        h = F.relu(self.gcn1(node_features, edge_index, edge_weight))
        h = F.dropout(h, p=self.dropout, training=self.training)
        h = F.relu(self.gcn2(h, edge_index, edge_weight))
        h = F.dropout(h, p=self.dropout, training=self.training)
        return self.gcn3(h, edge_index, edge_weight)

    def forward(
        self,
        node_feature_sequence: Tensor,
        edge_index: Tensor,
        edge_weight: Tensor | None = None,
    ) -> Tensor:
        """Return node embeddings for a [T, N, F] feature history."""
        if node_feature_sequence.dim() == 2:
            node_feature_sequence = node_feature_sequence.unsqueeze(0)
        if node_feature_sequence.dim() != 3:
            raise ValueError("node_feature_sequence must be [T, N, F] or [N, F]")

        slices = [
            self.encode(node_feature_sequence[t], edge_index, edge_weight)
            for t in range(node_feature_sequence.size(0))
        ]
        # [N, T, C]: GRU consumes each node's own trajectory over the window.
        stacked = torch.stack(slices, dim=1)
        output, _ = self.temporal(stacked)
        return output[:, -1, :]

    def score_transitions(
        self,
        embeddings: Tensor,
        source_index: int,
        target_indices: Tensor,
        normalised_distance: Tensor,
        normalised_transit: Tensor,
    ) -> Tensor:
        """sigma(f_STGCN(...)) for one source against its downstream nodes."""
        if target_indices.numel() == 0:
            return torch.empty(0, device=embeddings.device, dtype=embeddings.dtype)

        source = embeddings[source_index].unsqueeze(0).expand(target_indices.numel(), -1)
        targets = embeddings[target_indices]
        pair_features = torch.cat(
            [
                source,
                targets,
                source * targets,
                normalised_distance.unsqueeze(1),
                normalised_transit.unsqueeze(1),
            ],
            dim=1,
        )
        return torch.sigmoid(self.edge_scorer(pair_features).squeeze(1))


class PredictiveHandoffManager:
    """Combines the STGCN prior with kinematic and heading likelihoods."""

    def __init__(
        self,
        graph: SpatialCameraGraph,
        model: STGCNPredictor,
        *,
        probability_threshold: float = DEFAULT_PROBABILITY_THRESHOLD,
        sigma_t_seconds: float = 8.0,
        device: torch.device | str | None = None,
        history_window: int = 4,
    ) -> None:
        if not 0.0 < probability_threshold <= 1.0:
            raise ValueError("probability_threshold must be in (0, 1]")
        if sigma_t_seconds <= 0:
            raise ValueError("sigma_t_seconds must be positive")
        if history_window < 1:
            raise ValueError("history_window must be at least 1")

        self.device = torch.device(
            device if device is not None else ("cuda" if torch.cuda.is_available() else "cpu")
        )
        self.graph = graph.to(self.device)
        self.model = model.to(self.device).eval()
        self.probability_threshold = float(probability_threshold)
        self.sigma_t_seconds = float(sigma_t_seconds)
        self.history_window = int(history_window)

        self._embeddings: Tensor | None = None

    @torch.no_grad()
    def refresh_embeddings(self, node_feature_history: Tensor | None = None) -> Tensor:
        """Recompute node embeddings; call after graph or feature updates."""
        if self.graph.num_nodes == 0:
            raise RuntimeError("camera graph is empty; call SpatialCameraGraph.load() first")

        if node_feature_history is None:
            base = self.graph.node_features
            node_feature_history = base.unsqueeze(0).repeat(self.history_window, 1, 1)

        node_feature_history = node_feature_history.to(self.device)
        self._embeddings = self.model(
            node_feature_history, self.graph.edge_index, self.graph.edge_weight
        )
        return self._embeddings

    @torch.no_grad()
    def predict_next_cameras(
        self,
        current_camera_id: str,
        velocity: float,
        heading_degrees: float,
        elapsed_time_sec: float,
    ) -> list[tuple[str, float]]:
        """Rank downstream cameras and keep those at or above the threshold."""
        if velocity < MIN_VELOCITY_MPS:
            logger.debug(
                "velocity %.3f m/s below tracking floor; no handoff prediction", velocity
            )
            return []
        if elapsed_time_sec < 0:
            raise ValueError("elapsed_time_sec cannot be negative")

        edge_positions = self.graph.outgoing_edge_positions(current_camera_id)
        if not edge_positions:
            return []

        if self._embeddings is None:
            self.refresh_embeddings()
        embeddings = self._embeddings
        assert embeddings is not None  # refreshed above

        positions = torch.tensor(edge_positions, dtype=torch.long, device=self.device)
        edge_attr = self.graph.edge_attr[positions]
        distances = edge_attr[:, 0]
        transits = edge_attr[:, 1]
        bearings = edge_attr[:, 2]

        target_indices = self.graph.edge_index[1, positions]
        source_index = self.graph.node_index[current_camera_id]

        stgcn_prior = self.model.score_transitions(
            embeddings,
            source_index,
            target_indices,
            distances / self.graph.distance_threshold_m,
            transits / max(self.sigma_t_seconds, 1e-6),
        )

        # exp(-((dt - d_ij / ||v||)^2) / (2 * sigma_t^2))
        expected_arrival = distances / float(velocity)
        temporal_term = torch.exp(
            -torch.pow(float(elapsed_time_sec) - expected_arrival, 2)
            / (2.0 * self.sigma_t_seconds**2)
        )

        # (1 + cos(theta, u_ij)) / 2 with compass bearings in degrees.
        heading_rad = math.radians(float(heading_degrees) % 360.0)
        directional_term = (
            1.0 + torch.cos(heading_rad - torch.deg2rad(bearings))
        ) / 2.0

        probabilities = stgcn_prior * temporal_term * directional_term
        probabilities = torch.clamp(probabilities, 0.0, 1.0)

        keep = probabilities >= self.probability_threshold
        if not bool(torch.any(keep)):
            return []

        kept_probabilities = probabilities[keep]
        kept_targets = target_indices[keep]
        order = torch.argsort(kept_probabilities, descending=True)

        return [
            (
                self.graph.nodes[int(kept_targets[i])].camera_id,
                float(kept_probabilities[i]),
            )
            for i in order.tolist()
        ]

    def predict_batch(
        self,
        requests: Iterable[tuple[str, float, float, float]],
    ) -> dict[str, list[tuple[str, float]]]:
        """Convenience batch wrapper keyed by source camera_id."""
        results: dict[str, list[tuple[str, float]]] = {}
        for camera_id, velocity, heading, elapsed in requests:
            try:
                results[camera_id] = self.predict_next_cameras(
                    camera_id, velocity, heading, elapsed
                )
            except KeyError:
                logger.warning("skipping unknown camera_id in batch: %s", camera_id)
                results[camera_id] = []
        return results
