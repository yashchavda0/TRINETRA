"""Recompute `camera_transition_stats` from observed detection history.

Run with:
    python -m workers.seed_transition_stats --days 7 --min-samples 3

Model 4's handoff prior needs tau_ij, the expected travel time between a camera
pair. Until this table has rows, `engine/tracking_pipeline.py` falls back to
`d_ij / 11.1 m/s` for every pair in the fleet — one arterial-speed guess applied
to expressways and market lanes alike. This script replaces that guess with the
measured median of real target handoffs.

A hop is two consecutive detections of the same `target_id` on two different
cameras. Hops outside the plausibility window are discarded: sub-second hops are
clock skew or duplicate publishes, and very long ones are two unrelated
journeys that happen to share an identity.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from typing import Final

from app import database
from app.config import get_settings
from app.main import JsonLogFormatter

logger: Final = logging.getLogger("workers.seed_transition_stats")

_RECOMPUTE_SQL: Final = """
    WITH ordered AS (
        SELECT
            target_id,
            camera_id,
            timestamp_utc_ms,
            LAG(camera_id)        OVER (PARTITION BY target_id ORDER BY timestamp_utc_ms) AS prev_camera_id,
            LAG(timestamp_utc_ms) OVER (PARTITION BY target_id ORDER BY timestamp_utc_ms) AS prev_timestamp_utc_ms
        FROM detections
        WHERE target_id IS NOT NULL
          AND timestamp_utc_ms >= $1
    ),
    hops AS (
        SELECT
            prev_camera_id AS source_camera_id,
            camera_id      AS target_camera_id,
            (timestamp_utc_ms - prev_timestamp_utc_ms) / 1000.0 AS transit_seconds
        FROM ordered
        WHERE prev_camera_id IS NOT NULL
          AND prev_camera_id <> camera_id
          AND timestamp_utc_ms > prev_timestamp_utc_ms
          AND (timestamp_utc_ms - prev_timestamp_utc_ms) / 1000.0 BETWEEN $2 AND $3
    )
    INSERT INTO camera_transition_stats AS stats (
        source_camera_id, target_camera_id, median_transit_seconds, sample_count, updated_at
    )
    SELECT
        hops.source_camera_id,
        hops.target_camera_id,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY hops.transit_seconds),
        count(*),
        clock_timestamp()
    FROM hops
    WHERE EXISTS (SELECT 1 FROM cameras c WHERE c.id = hops.source_camera_id)
      AND EXISTS (SELECT 1 FROM cameras c WHERE c.id = hops.target_camera_id)
    GROUP BY hops.source_camera_id, hops.target_camera_id
    HAVING count(*) >= $4
    ON CONFLICT (source_camera_id, target_camera_id) DO UPDATE
    SET median_transit_seconds = EXCLUDED.median_transit_seconds,
        sample_count           = EXCLUDED.sample_count,
        updated_at             = clock_timestamp()
"""


def _configure_logging(log_level: str) -> None:
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recompute camera_transition_stats from the detections table."
    )
    parser.add_argument(
        "--days",
        type=float,
        default=7.0,
        help="How far back to read detection history (default: 7)",
    )
    parser.add_argument(
        "--min-transit-seconds",
        type=float,
        default=2.0,
        help="Discard hops faster than this; below it the gap is clock skew (default: 2)",
    )
    parser.add_argument(
        "--max-transit-seconds",
        type=float,
        default=1_800.0,
        help="Discard hops slower than this; above it the two sightings are separate journeys (default: 1800)",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=3,
        help="Minimum observed hops before a pair's median is trusted (default: 3)",
    )
    return parser.parse_args(argv)


async def recompute(args: argparse.Namespace) -> int:
    settings = get_settings()
    _configure_logging(settings.log_level)

    if args.min_transit_seconds <= 0:
        raise SystemExit("--min-transit-seconds must be positive")
    if args.max_transit_seconds <= args.min_transit_seconds:
        raise SystemExit("--max-transit-seconds must exceed --min-transit-seconds")

    since_ms = int((time.time() - args.days * 86_400.0) * 1000)

    await database.connect(settings)
    try:
        pool = database.get_pool()
        async with pool.acquire() as connection:
            status = await connection.execute(
                _RECOMPUTE_SQL,
                since_ms,
                args.min_transit_seconds,
                args.max_transit_seconds,
                args.min_samples,
            )
            total = await connection.fetchval(
                "SELECT count(*) FROM camera_transition_stats"
            )
    finally:
        await database.disconnect()

    logger.info(
        "transition stats recomputed",
        extra={
            "command_status": status,
            "rows_in_table": int(total or 0),
            "window_days": args.days,
            "min_samples": args.min_samples,
        },
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(recompute(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
