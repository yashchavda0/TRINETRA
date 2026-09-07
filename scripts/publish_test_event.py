"""Publish synthetic SurveillanceEvent messages onto the analytics bus.

Stands in for cmd/ingestion_gateway so the Python tier can be exercised end to
end without Go, ffmpeg or mTLS certificates.

Examples:
    # one detection on a registered camera
    python scripts/publish_test_event.py --camera-id <uuid> --lat 23.0225 --lon 72.5714

    # a two-camera handoff for the same target, 12 seconds apart
    python scripts/publish_test_event.py --camera-id <uuid-a> --lat 23.0225 --lon 72.5714 --track-id T-1
    python scripts/publish_test_event.py --camera-id <uuid-b> --lat 23.0290 --lon 72.5760 --track-id T-1 --offset-seconds 12

    # a plate read, which triggers registry screening and a possible P0 alert
    python scripts/publish_test_event.py --camera-id <uuid> --lat 23.0225 --lon 72.5714 --plate GJ01AB1234

    # a contract violation the worker must reject rather than store
    python scripts/publish_test_event.py --camera-id <uuid> --lat 23.0225 --lon 72.5714 --embedding-dim 128
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
import uuid
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from confluent_kafka import Producer
except ImportError:  # pragma: no cover - dependency guard
    raise SystemExit("confluent-kafka is not installed; run: pip install -r requirements.txt")

try:
    from generated import surveillance_event_pb2 as pb
except ImportError:  # pragma: no cover - codegen guard
    raise SystemExit(
        "generated/surveillance_event_pb2.py is missing. Run scripts/gen_proto.ps1 first."
    )


def build_embedding(dimension: int, seed: str) -> list[float]:
    """Deterministic L2-normalised vector, so repeat runs re-identify each other."""
    base = [
        math.sin((hash((seed, i)) % 10_000) / 10_000.0 * math.pi) for i in range(dimension)
    ]
    norm = math.sqrt(sum(value * value for value in base)) or 1.0
    return [value / norm for value in base]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--camera-id", required=True, help="Registry UUID of the producing camera")
    parser.add_argument("--lat", type=float, required=True, help="WGS84 latitude of the camera")
    parser.add_argument("--lon", type=float, required=True, help="WGS84 longitude of the camera")
    parser.add_argument("--azimuth", type=float, default=90.0, help="Camera bearing in degrees (default: 90)")
    parser.add_argument("--department", default="POLICE", help="DepartmentCode enum name (default: POLICE)")
    parser.add_argument("--object-class", default="AUTOMOBILE", help="ObjectClass enum name (default: AUTOMOBILE)")
    parser.add_argument("--track-id", default=None, help="Stable track id; also seeds the embedding")
    parser.add_argument("--plate", default=None, help="license_plate attribute, e.g. GJ01AB1234")
    parser.add_argument("--colour", default="white", help="color attribute (default: white)")
    parser.add_argument("--count", type=int, default=1, help="How many events to publish (default: 1)")
    parser.add_argument(
        "--offset-seconds",
        type=float,
        default=0.0,
        help="Shift timestamp_utc_ms forward by this many seconds (default: 0)",
    )
    parser.add_argument(
        "--embedding-dim",
        type=int,
        default=512,
        help="Feature vector length. Anything but 512 must be rejected by the worker (default: 512)",
    )
    parser.add_argument(
        "--bootstrap",
        default=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:9092"),
        help="Kafka bootstrap servers (default: $KAFKA_BOOTSTRAP_SERVERS or 127.0.0.1:9092)",
    )
    parser.add_argument(
        "--topic",
        default=os.getenv("KAFKA_TOPIC_RAW", "surveillance-events-raw"),
        help="Target topic (default: $KAFKA_TOPIC_RAW or surveillance-events-raw)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        uuid.UUID(args.camera_id)
    except ValueError:
        raise SystemExit(f"--camera-id must be a UUID from the registry: {args.camera_id!r}")

    try:
        department = pb.DepartmentCode.Value(args.department.upper())
        object_class = pb.ObjectClass.Value(args.object_class.upper())
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    producer = Producer({"bootstrap.servers": args.bootstrap})
    published = 0

    for index in range(args.count):
        track_id = args.track_id or f"T-{uuid.uuid4().hex[:8]}"
        event = pb.SurveillanceEvent(
            event_id=str(uuid.uuid4()),
            camera_id=args.camera_id,
            department_code=department,
            timestamp_utc_ms=int((time.time() + args.offset_seconds) * 1000) + index,
            location=pb.GeoLocation(
                latitude=args.lat, longitude=args.lon, azimuth_degrees=args.azimuth
            ),
            bounding_box=pb.BBox(x_min=0.31, y_min=0.42, x_max=0.55, y_max=0.78),
            object_class=object_class,
            feature_embedding=build_embedding(args.embedding_dim, track_id),
        )
        event.attributes["track_id"] = track_id
        event.attributes["color"] = args.colour
        event.attributes["confidence"] = "0.93"
        if args.plate:
            event.attributes["license_plate"] = args.plate.upper()
            event.attributes["license_plate_conf"] = "0.88"

        producer.produce(
            args.topic,
            key=str(int(department)),
            value=event.SerializeToString(),
            headers=[("event_id", event.event_id), ("camera_id", event.camera_id)],
        )
        published += 1
        print(
            f"queued event_id={event.event_id} track_id={track_id} "
            f"embedding_dim={args.embedding_dim}"
        )

    remaining = producer.flush(10.0)
    if remaining:
        print(f"WARNING: {remaining} message(s) were not delivered", file=sys.stderr)
        return 1

    print(f"published {published} event(s) to {args.topic} via {args.bootstrap}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
