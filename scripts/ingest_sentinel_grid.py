"""Ingest cameras from Gujarat Police Sentinel Camera Grid into TRINETRA.

Reads the camera catalogue from http://<host>/api/ingest (as specified in the
Sentinel Integrator's Guide at https://sentinel.gujarat.gov.in/resource) or from
a local JSON dump, and registers each camera with Model 1 Central GIS Registry.

Usage:
    python scripts/ingest_sentinel_grid.py --host <host-ip-or-domain>
    python scripts/ingest_sentinel_grid.py --url http://<host>/api/ingest
    python scripts/ingest_sentinel_grid.py --file sandbox_cameras.json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any
import httpx


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest Sentinel Camera Grid into TRINETRA registry."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--host",
        help="Sentinel sandbox host (e.g. 10.20.30.40 or sandbox.sentinel.gujarat.gov.in:8080)",
    )
    group.add_argument(
        "--url",
        help="Full URL to Sentinel catalogue endpoint (e.g. http://<host>/api/ingest)",
    )
    group.add_argument(
        "--file",
        help="Path to a local JSON file containing the /api/ingest response",
    )
    parser.add_argument(
        "--api-url",
        default="http://127.0.0.1:8000",
        help="TRINETRA API base URL (default: http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--department",
        default="POLICE",
        help="Department code for registered cameras (default: POLICE)",
    )
    return parser.parse_args()


def fetch_catalogue(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        url = args.url or f"http://{args.host}/api/ingest"
        if not url.startswith(("http://", "https://")):
            url = f"http://{url}"
        print(f"Fetching camera catalogue from: {url}")
        resp = httpx.get(url, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()

    # The catalogue may be a list directly or wrapped under 'cameras' / 'items'
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("cameras", "items", "data"):
            if key in data and isinstance(data[key], list):
                return data[key]
    raise ValueError(f"Unexpected catalogue payload format: {type(data)}")


def register_camera(
    api_url: str,
    cam: dict[str, Any],
    default_department: str,
    host: str | None = None,
) -> bool:
    cam_id = str(cam.get("id") or cam.get("camera_id") or cam.get("code") or "CAM")
    code = cam.get("global_camera_code") or f"SENTINEL-{cam_id}"

    # Extract coordinates
    location = cam.get("location") or {}
    if isinstance(location, dict):
        lat = location.get("lat") or location.get("latitude") or cam.get("latitude")
        lon = location.get("lon") or location.get("longitude") or cam.get("longitude")
    elif isinstance(location, (list, tuple)) and len(location) >= 2:
        lon, lat = location[0], location[1]
    else:
        lat = cam.get("latitude") or 23.0225
        lon = cam.get("longitude") or 72.5714

    # Extract RTSP URL
    stream_url = (
        cam.get("stream_url")
        or cam.get("rtsp_url")
        or cam.get("rtsp")
        or (cam.get("urls", {}).get("rtsp") if isinstance(cam.get("urls"), dict) else None)
    )
    if not stream_url and host:
        stream_url = f"rtsp://{host}:8554/stream/{cam_id}"

    payload = {
        "global_camera_code": code,
        "department_id": cam.get("department_id") or default_department,
        "latitude": float(lat),
        "longitude": float(lon),
        "azimuth_angle": float(cam.get("azimuth_angle") or cam.get("azimuth") or 0.0),
        "stream_url": stream_url or f"rtsp://sentinel-grid:8554/stream/{cam_id}",
        "vms_vendor": "Sentinel-Grid",
        "description": f"Sentinel Sandbox Camera {cam_id} ({cam.get('codec', 'H264')})",
    }

    try:
        resp = httpx.post(f"{api_url}/api/v1/cameras", json=payload, timeout=5.0)
        if resp.status_code in (200, 201):
            res = resp.json()
            print(f"  [OK] Registered {code} (ID: {res.get('id')}) -> {payload['stream_url']}")
            return True
        elif resp.status_code == 409:
            print(f"  [EXISTS] {code} already registered")
            return True
        else:
            print(f"  [FAIL] {code}: HTTP {resp.status_code} - {resp.text}")
            return False
    except Exception as exc:
        print(f"  [ERROR] {code}: {exc}")
        return False


def main() -> int:
    args = parse_args()
    print("=== TRINETRA: Sentinel Camera Grid Onboarding ===")
    try:
        cameras = fetch_catalogue(args)
    except Exception as exc:
        print(f"Error fetching catalogue: {exc}")
        return 1

    print(f"Found {len(cameras)} cameras in Sentinel catalogue.")
    registered = 0
    for cam in cameras:
        if register_camera(args.api_url, cam, args.department, host=args.host):
            registered += 1

    print(f"\nSuccessfully registered {registered}/{len(cameras)} cameras.")
    print("View all cameras live on the GIS Console: http://localhost:5173")
    return 0


if __name__ == "__main__":
    sys.exit(main())
