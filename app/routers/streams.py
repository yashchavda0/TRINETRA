"""Live-stream signalling proxy between the GIS console and the media plane.

The console speaks plain WebRTC signalling: `POST /api/v2/webrtc/offer` with
`{camera_id, sdp, type}`, expecting `{type, sdp}` back. This module translates
that into whichever media backend is configured:

* **MediaMTX (default).** Forwards the offer to MediaMTX's WHEP endpoint and
  returns its answer. MediaMTX owns ICE, DTLS and SRTP, so this is the path that
  actually delivers pictures.
* **cmd/stream_relay (legacy).** The original Go service. Kept working for
  deployments that require it, with the caveat that its media path is not
  functional - see the README's known limitations.

Two things this module owns regardless of backend:

1. **`rtsp_url` resolution.** The stream URL is looked up from the registry by
   `camera_id`, never accepted from the caller. A console user can therefore only
   open a stream for a registered camera, and the credential-bearing RTSP URL
   never reaches the browser.
2. **Session bookkeeping.** Every negotiated session is recorded in
   `relay_sessions` so it survives a restart and can be reaped.

A note on ICE, because it constrains everything here: the console posts its offer
*before* ICE gathering completes and never sends a candidate afterwards
(`GISMap.jsx:507-517` has no `onicecandidate` handler). The answer must therefore
carry candidates the browser can reach on its own. That is why `mediamtx.yml`
advertises 127.0.0.1 rather than the container's address.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Final
from urllib.parse import quote, urlparse, urlunparse

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.auth.dependencies import require_role
from app.config import Settings, get_settings
from app.database import get_connection, get_pool
from app.schemas import (
    RecordingSegment,
    RecordingsResponse,
    StreamStateResponse,
    WebRTCAnswerResponse,
    WebRTCOfferRequest,
)

logger: Final = logging.getLogger(__name__)

# Authentication is applied at the router, not per route, so a new streaming
# endpoint is protected by default. Until this was added the registry was locked
# while the video plane beside it was open: anyone who could reach the port
# could open a live stream for any camera, and the camera's stream_url - which
# frequently carries credentials - is resolved server-side for whoever asks.
# VIEWER is the floor deliberately: watching is the least privileged thing an
# authenticated operator does.
router = APIRouter(
    prefix="/api/v2",
    tags=["streams"],
    dependencies=[Depends(require_role("VIEWER"))],
)

# Module-level clients so TLS handshakes and connection setup are amortised
# across requests rather than repeated per offer.
_relay_client: httpx.AsyncClient | None = None
_media_client: httpx.AsyncClient | None = None
_client_lock: Final = asyncio.Lock()
_keepalive_task: asyncio.Task[None] | None = None


# ---------------------------------------------------------------------------
# HTTP clients
# ---------------------------------------------------------------------------


async def _get_media_client(settings: Settings) -> httpx.AsyncClient:
    """Pooled plain-HTTP client for MediaMTX (loopback, no TLS)."""
    global _media_client

    if _media_client is None:
        async with _client_lock:
            if _media_client is None:
                _media_client = httpx.AsyncClient(
                    timeout=httpx.Timeout(settings.mediamtx_request_timeout_seconds),
                    follow_redirects=True,
                )
    return _media_client


async def _get_relay_client(settings: Settings) -> httpx.AsyncClient:
    """Pooled mTLS client for the legacy Go relay."""
    global _relay_client

    if _relay_client is None:
        async with _client_lock:
            if _relay_client is None:
                if not settings.stream_relay_base_url:
                    raise RuntimeError("stream_relay_base_url is not configured")
                _relay_client = httpx.AsyncClient(
                    base_url=settings.stream_relay_base_url.rstrip("/"),
                    cert=settings.relay_client_cert,
                    verify=settings.relay_ca_file or True,
                    timeout=httpx.Timeout(settings.relay_request_timeout_seconds),
                    headers={"Accept": "application/json"},
                )
    return _relay_client


async def aclose_clients() -> None:
    """Close both backend clients during application shutdown."""
    global _media_client, _relay_client

    for name in ("_media_client", "_relay_client"):
        client = globals()[name]
        if client is not None:
            globals()[name] = None
            await client.aclose()


# ---------------------------------------------------------------------------
# MediaMTX path resolution
# ---------------------------------------------------------------------------


def _path_from_stream_url(stream_url: str) -> str | None:
    """Return the MediaMTX path when `stream_url` already points at MediaMTX."""
    parsed = urlparse(stream_url)
    path = parsed.path.lstrip("/")
    return path or None


def _is_mediamtx_source(stream_url: str, settings: Settings) -> bool:
    """True when the camera publishes directly to our MediaMTX instance."""
    parsed = urlparse(stream_url)
    if parsed.scheme.lower() not in {"rtsp", "rtsps"}:
        return False

    host = (parsed.hostname or "").lower()
    port = parsed.port or 554
    configured_host, _, configured_port = settings.mediamtx_rtsp_host.partition(":")
    configured_host = configured_host.lower()

    # Treat the loopback spellings as the same host: a camera row may say
    # localhost while the setting says 127.0.0.1.
    loopback = {"127.0.0.1", "localhost", "::1", "mediamtx"}
    host_matches = host == configured_host or (
        host in loopback and configured_host in loopback
    )
    return host_matches and str(port) == (configured_port or "8554")


def _with_grid_credentials(stream_url: str, settings: Settings) -> str:
    """Return `stream_url` with the live grid's credentials attached, in memory.

    The grid authenticates every RTSP and WebRTC connection with a registered
    email and access password embedded in the URL, so they have to be present
    when MediaMTX dials the camera. They must not be present anywhere else:
    registry rows for grid cameras are stored without credentials, which is
    what keeps the password out of Postgres and out of every response from
    GET /api/v1/cameras. This function is that seam, and the returned value
    never leaves the process except towards the media server.

    The credentials are only ever attached to a host on the configured grid
    allowlist. Appending them to whatever URL a camera row happens to hold
    would hand our password to any third-party endpoint someone registered.
    """
    credentials = settings.live_grid_credentials
    if credentials is None:
        return stream_url

    parsed = urlparse(stream_url)
    if not parsed.hostname or parsed.hostname.lower() not in settings.live_grid_media_hosts:
        return stream_url
    # An explicit userinfo in the row wins: it is an operator's deliberate
    # override, possibly for a camera with its own account.
    if parsed.username or parsed.password:
        return stream_url

    email, password = credentials
    # quote() is what turns the email's @ into %40, as the grid requires, and
    # it also survives a password containing : / @ or #.
    userinfo = f"{quote(email, safe='')}:{quote(password, safe='')}"
    netloc = f"{userinfo}@{parsed.netloc}"
    # MediaMTX keeps this source string in its own path config, so it is
    # visible on the control API - which is loopback-bound and already grants
    # full control to anyone who reaches it, so this adds no new exposure.
    return urlunparse(parsed._replace(netloc=netloc))


def _rtsp_endpoint(stream_url: str) -> str:
    """`rtsp://host:port` with credentials and path stripped, for error text.

    Never echo the full stream_url to a client: it carries camera credentials.
    """
    parsed = urlparse(stream_url)
    scheme = parsed.scheme or "rtsp"
    host = parsed.hostname or "?"
    if parsed.port:
        return f"{scheme}://{host}:{parsed.port}"
    # A camera can now be registered by its HLS URL, which normally carries no
    # port. Naming RTSP's 554 there would send the reader hunting for a port
    # that was never in play.
    port = 554 if scheme in {"rtsp", "rtsps"} else None
    return f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"


def _media_unreachable(settings: Settings, exc: Exception) -> HTTPException:
    """502 for a media server that is genuinely not answering."""
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=(
            "media server is unreachable at "
            f"{settings.mediamtx_whep_base_url} - is the mediamtx container running?"
        ),
    )


async def _describe_path(path_name: str, settings: Settings) -> str:
    """MediaMTX's own view of a path, for inclusion in a timeout message.

    Asking the server what it observed beats reporting what our HTTP client
    inferred: it distinguishes "never connected to the camera" from "connected
    but produced no media".
    """
    client = await _get_media_client(settings)
    api_base = (settings.mediamtx_api_base_url or "").rstrip("/")
    try:
        response = await client.get(
            f"{api_base}/v3/paths/get/{path_name}",
            timeout=httpx.Timeout(settings.mediamtx_request_timeout_seconds),
        )
        if response.status_code != status.HTTP_200_OK:
            return "media server has no record of the path"
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return "media server did not report the path state"

    ready = bool(body.get("ready"))
    if ready:
        return "the path is ready but produced no media in time"
    return "the camera source never became ready"


async def _apply_path_config(
    path_name: str, config: dict[str, object], camera_id: uuid.UUID, settings: Settings
) -> None:
    """POST a new MediaMTX path config, falling back to PATCH when it exists.

    Shared by the on-demand viewing path and the continuous recording path:
    both configure a `cam-<uuid>` path, they just choose different values for
    `sourceOnDemand`/`record`/etc. Raises on any failure; callers that can
    tolerate one camera's config failing (the recording reconciler) catch
    around this rather than this function swallowing anything itself.
    """
    client = await _get_media_client(settings)
    api_base = (settings.mediamtx_api_base_url or "").rstrip("/")
    control_timeout = httpx.Timeout(settings.mediamtx_request_timeout_seconds)

    try:
        response = await client.post(
            f"{api_base}/v3/config/paths/add/{path_name}",
            json=config,
            timeout=control_timeout,
        )

        # "path already exists" is the normal second-viewer (or second
        # reconcile tick) case, but the stored config may be stale - a
        # corrected stream_url or retention_days would otherwise never take
        # effect.
        if response.status_code >= 400 and "already exists" in response.text.lower():
            logger.debug(
                "media path exists; refreshing its config",
                extra={"camera_id": str(camera_id), "path": path_name},
            )
            # This endpoint is registered under the HTTP PATCH verb only -
            # POSTing to it returns "404 page not found" from the router, which
            # looks like a missing path rather than a wrong method. Verified
            # against MediaMTX v1.9.3.
            response = await client.patch(
                f"{api_base}/v3/config/paths/patch/{path_name}",
                json=config,
                timeout=control_timeout,
            )
    except httpx.HTTPError as exc:
        # The control API is on loopback and answers in milliseconds, so ANY
        # transport failure here means the media server is not there. Matching
        # on ConnectError alone is not enough: a stopped container behind
        # Docker's port proxy resets the connection instead of refusing it,
        # which surfaces as ReadError or RemoteProtocolError.
        logger.error(
            "mediamtx control API unreachable",
            extra={
                "camera_id": str(camera_id),
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
        )
        raise _media_unreachable(settings, exc) from exc

    if response.status_code >= 400:
        logger.error(
            "mediamtx rejected path configuration",
            extra={
                "camera_id": str(camera_id),
                "status_code": response.status_code,
                "body": response.text[:200],
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"media server refused the camera path ({response.status_code})",
        )


async def _ensure_pull_path(
    camera_id: uuid.UUID, stream_url: str, settings: Settings
) -> str:
    """Create or refresh an on-demand MediaMTX path pulling from a camera.

    Used for a camera that is not being continuously recorded: MediaMTX only
    opens its stream while a viewer is watching. A camera whose recording
    reconciler already keeps its path up continuously must not go through
    here - see the `recording_enabled` check in `_negotiate_via_mediamtx`.
    """
    path_name = f"cam-{camera_id}"
    config = {
        "source": stream_url,
        "sourceOnDemand": True,
        "sourceProtocol": "tcp",
    }
    await _apply_path_config(path_name, config, camera_id, settings)
    return path_name


async def _ensure_recording_path(
    camera_id: uuid.UUID, stream_url: str, retention_days: int, settings: Settings
) -> str:
    """Create or refresh a continuously-recording MediaMTX path for a camera.

    `sourceOnDemand: False` is the whole point: recording needs the source
    running whether or not anyone is watching, unlike `_ensure_pull_path`'s
    on-demand viewing case. A live viewer for this camera becomes just another
    reader of the path this creates - see `_negotiate_via_mediamtx`.

    `recordDeleteAfter` is MediaMTX's own retention mechanism, computed from
    the camera's `retention_days` (or the configured default when unset). It
    operates at segment granularity, so actual retention can run up to one
    segment duration longer than requested - documented behaviour, not a bug
    here.
    """
    path_name = f"cam-{camera_id}"
    config = {
        "source": stream_url,
        "sourceOnDemand": False,
        "sourceProtocol": "tcp",
        "record": True,
        "recordPath": f"{settings.recordings_dir}/{camera_id}/%Y-%m-%d_%H-%M-%S-%f",
        "recordFormat": settings.recording_format,
        "recordSegmentDuration": f"{settings.recording_segment_seconds}s",
        "recordDeleteAfter": f"{retention_days * 24}h",
    }
    await _apply_path_config(path_name, config, camera_id, settings)
    return path_name


# ---------------------------------------------------------------------------
# Offer handling
# ---------------------------------------------------------------------------


async def _negotiate_via_mediamtx(
    payload: WebRTCOfferRequest,
    stream_url: str,
    settings: Settings,
    *,
    recording_enabled: bool,
) -> tuple[str, str, str]:
    """Run the WHEP exchange. Returns (answer_sdp, session_id, resource_url)."""
    is_published = _is_mediamtx_source(stream_url, settings)
    if is_published:
        path_name = _path_from_stream_url(stream_url)
        if not path_name:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="camera stream_url names no MediaMTX path",
            )
    elif recording_enabled:
        # The recording reconciler already keeps this path up continuously
        # (sourceOnDemand: False, record: True). Re-asserting _ensure_pull_path's
        # on-demand config here would flip it back to on-demand and silently
        # stop recording until the next reconcile tick re-applies it - so a
        # viewer clicking a camera must not touch its path config at all, only
        # read from the path that already exists.
        path_name = f"cam-{payload.camera_id}"
    else:
        path_name = await _ensure_pull_path(payload.camera_id, stream_url, settings)

    client = await _get_media_client(settings)
    whep_base = (settings.mediamtx_whep_base_url or "").rstrip("/")
    whep_url = f"{whep_base}/{path_name}/whep"

    try:
        # WHEP is SDP-over-HTTP: the offer is the raw body, not JSON. A pull path
        # gets the longer budget because MediaMTX has to bring the camera up
        # before it can answer.
        response = await client.post(
            whep_url,
            content=payload.sdp.encode("utf-8"),
            headers={"Content-Type": "application/sdp", "Accept": "application/sdp"},
            timeout=httpx.Timeout(
                settings.mediamtx_request_timeout_seconds
                if is_published
                else settings.mediamtx_source_start_timeout_seconds
            ),
        )
    except httpx.TimeoutException as exc:
        # MediaMTX accepted the request but could not produce media in time,
        # which in practice means the camera's own RTSP endpoint is unreachable.
        budget = (
            settings.mediamtx_request_timeout_seconds
            if is_published
            else settings.mediamtx_source_start_timeout_seconds
        )
        observed = await _describe_path(path_name, settings)
        endpoint = _rtsp_endpoint(stream_url)
        logger.warning(
            "camera source did not start streaming",
            extra={
                "camera_id": str(payload.camera_id),
                "path": path_name,
                "rtsp_endpoint": endpoint,
                "timeout_seconds": budget,
                "observed": observed,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=(
                f"camera source {endpoint} did not start streaming within "
                f"{budget:g}s: {observed}. The media server is up - check that "
                "the camera is reachable and its stream_url is correct."
            ),
        ) from exc
    except httpx.HTTPError as exc:
        # Not a timeout, so not the camera being slow: the signalling transport
        # itself failed, which means the media server is gone.
        logger.error(
            "whep exchange failed",
            extra={
                "camera_id": str(payload.camera_id),
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
        )
        raise _media_unreachable(settings, exc) from exc

    if response.status_code not in (status.HTTP_200_OK, status.HTTP_201_CREATED):
        body = response.text[:300]
        lowered = body.lower()
        logger.warning(
            "mediamtx refused the session",
            extra={
                "camera_id": str(payload.camera_id),
                "path": path_name,
                "status_code": response.status_code,
                "body": body,
            },
        )

        # MediaMTX does not hold the request open when a pull source fails to
        # come up: it answers 400 with "source of path '...' has timed out".
        # That is the camera's fault, not the media server's, so it must not be
        # reported as a 502 - which is what sent the last investigation to the
        # wrong component.
        if "timed out" in lowered or "not ready" in lowered:
            endpoint = _rtsp_endpoint(stream_url)
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=(
                    f"camera source {endpoint} did not start streaming: the media "
                    "server is up but the camera did not respond. Check that the "
                    "camera is reachable and its stream_url is correct."
                ),
            )

        if response.status_code == status.HTTP_404_NOT_FOUND or "publish" in lowered:
            # The path exists in config but nothing is publishing to it.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"no live stream on media path '{path_name}'. Nothing is "
                    "publishing to it - check that the source is up."
                ),
            )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"media server refused the session (status {response.status_code})",
        )

    answer_sdp = response.text
    if "v=0" not in answer_sdp:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="media server returned something that is not an SDP answer",
        )

    # WHEP names the session resource in Location; it is what a DELETE targets.
    # Location is usually relative, so resolve it against the WHEP endpoint.
    location = response.headers.get("Location", "")
    resource_url = str(httpx.URL(whep_url).join(location)) if location else whep_url

    return answer_sdp, uuid.uuid4().hex, resource_url


async def _negotiate_via_relay(
    payload: WebRTCOfferRequest, stream_url: str, settings: Settings
) -> tuple[str, str, str]:
    """Legacy path: broker the offer through cmd/stream_relay over mTLS."""
    client = await _get_relay_client(settings)
    try:
        response = await client.post(
            "/api/v1/streams/play",
            json={
                "camera_id": str(payload.camera_id),
                "rtsp_url": stream_url,
                "sdp_offer": payload.sdp,
            },
        )
    except httpx.HTTPError as exc:
        logger.error(
            "relay unreachable",
            extra={"camera_id": str(payload.camera_id), "error": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="stream relay is unreachable",
        ) from exc

    if response.status_code != status.HTTP_200_OK:
        # The relay's body can name the camera's RTSP failure, but it also
        # contains the credential-bearing stream URL, so never echo it verbatim.
        logger.warning(
            "relay refused session",
            extra={
                "camera_id": str(payload.camera_id),
                "relay_status": response.status_code,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"stream relay refused the session (relay status {response.status_code})",
        )

    body = response.json()
    session_id = str(body.get("session_id") or "")
    sdp_answer = str(body.get("sdp_answer") or "")
    if not session_id or not sdp_answer:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="stream relay returned an incomplete session",
        )

    return sdp_answer, session_id, str(settings.stream_relay_base_url)


@router.post(
    "/webrtc/offer",
    response_model=WebRTCAnswerResponse,
    summary="Open a live stream for a registered camera",
)
async def webrtc_offer(
    payload: WebRTCOfferRequest,
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> WebRTCAnswerResponse:
    """Resolve the camera's stream URL, then negotiate with the media backend."""
    backend = settings.media_backend
    if backend == "none":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "live streaming is unavailable: neither MEDIAMTX_WHEP_BASE_URL nor "
                "STREAM_RELAY_BASE_URL is configured, so no media backend is deployed"
            ),
        )

    record = await connection.fetchrow(
        "SELECT stream_url, status, recording_enabled FROM cameras WHERE id = $1",
        payload.camera_id,
    )
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"camera '{payload.camera_id}' is not registered",
        )
    if record["status"] != "ACTIVE":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"camera '{payload.camera_id}' is {record['status']}, not ACTIVE",
        )

    stream_url = _with_grid_credentials(record["stream_url"], settings)
    if backend == "mediamtx":
        # Must agree with _reconcile_recording_paths' own WHERE clause, or a
        # camera the reconciler is about to bring up continuously could still
        # be flipped to on-demand by a viewer in the gap before its first tick.
        sdp_answer, session_id, resource = await _negotiate_via_mediamtx(
            payload,
            stream_url,
            settings,
            recording_enabled=bool(record["recording_enabled"]),
        )
    else:
        sdp_answer, session_id, resource = await _negotiate_via_relay(
            payload, stream_url, settings
        )

    await connection.execute(
        """
        INSERT INTO relay_sessions (session_id, camera_id, relay_addr, state)
        VALUES ($1, $2, $3, 'ACTIVE')
        ON CONFLICT (session_id) DO UPDATE
        SET state = 'ACTIVE',
            relay_addr = EXCLUDED.relay_addr,
            last_heartbeat_at = clock_timestamp()
        """,
        session_id,
        payload.camera_id,
        resource,
    )

    logger.info(
        "live session opened",
        extra={
            "camera_id": str(payload.camera_id),
            "session_id": session_id,
            "backend": backend,
        },
    )
    return WebRTCAnswerResponse(
        sdp=sdp_answer, session_id=session_id, camera_id=payload.camera_id
    )


@router.get(
    "/streams/{camera_id}/state",
    response_model=StreamStateResponse,
    summary="What the media server currently holds for a camera",
)
async def stream_state(
    camera_id: uuid.UUID,
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> StreamStateResponse:
    """Report MediaMTX's view of this camera's path, codecs included.

    This exists because the answer SDP does not say what is actually arriving.
    A camera publishing H.265 negotiates cleanly, delivers bytes, and shows a
    black tile in Chrome - which looks exactly like a dead camera. Asking the
    media server what tracks it sees turns that into a sentence the operator
    can act on.

    A path that does not exist yet is not an error: `sourceOnDemand` means the
    path is created on the first viewer, so `ready: false` is the normal answer
    before anyone has watched.
    """
    exists = await connection.fetchval("SELECT 1 FROM cameras WHERE id = $1", camera_id)
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"camera '{camera_id}' is not registered",
        )

    path_name = f"cam-{camera_id}"
    empty = StreamStateResponse(camera_id=camera_id, path=path_name, ready=False)

    api_base = (settings.mediamtx_api_base_url or "").rstrip("/")
    if not api_base:
        # No MediaMTX configured at all: report "nothing ready" rather than
        # failing, so the console can still show its own WebRTC statistics.
        return empty

    client = await _get_media_client(settings)
    try:
        response = await client.get(
            f"{api_base}/v3/paths/get/{path_name}",
            timeout=httpx.Timeout(settings.mediamtx_request_timeout_seconds),
        )
        if response.status_code != status.HTTP_200_OK:
            return empty
        body = response.json()
    except (httpx.HTTPError, ValueError):
        raise _media_unreachable(settings, RuntimeError("path state unavailable")) from None

    return StreamStateResponse(
        camera_id=camera_id,
        path=path_name,
        ready=bool(body.get("ready")),
        tracks=[str(track) for track in (body.get("tracks") or [])],
        ready_time=body.get("readyTime"),
    )


async def _list_recorded_segments(
    path_name: str, start: datetime, end: datetime, settings: Settings
) -> tuple[list[RecordingSegment], str | None]:
    """Ask MediaMTX's playback server what segments exist for a path/window.

    MediaMTX 1.9.3 ships a playback server (mediamtx.yml's `playback:` block)
    separate from the control API, but its exact query-string contract was not
    something this codebase's own testing could confirm without the server
    running - so this is written defensively: any shape mismatch is logged and
    reported back as an empty list with `note` set, never a 500. First thing to
    confirm once this is live is the real request/response shape against the
    running container, and adjust the query below to match.
    """
    base = (settings.mediamtx_playback_base_url or "").rstrip("/")
    if not base:
        return [], "playback server is not configured"

    client = await _get_media_client(settings)
    try:
        response = await client.get(
            f"{base}/list",
            params={
                "path": path_name,
                "start": start.astimezone(timezone.utc).isoformat(),
                "end": end.astimezone(timezone.utc).isoformat(),
            },
            timeout=httpx.Timeout(settings.mediamtx_request_timeout_seconds),
        )
        if response.status_code != status.HTTP_200_OK:
            return [], f"playback server returned {response.status_code}"
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(
            "recording segment list unavailable",
            extra={"path": path_name, "error": str(exc), "error_type": type(exc).__name__},
        )
        return [], "playback server did not answer"

    entries = body if isinstance(body, list) else body.get("items", [])
    segments: list[RecordingSegment] = []
    for entry in entries:
        try:
            segments.append(
                RecordingSegment(
                    start=entry["start"],
                    duration_seconds=float(entry.get("duration", 0)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue  # one malformed entry must not drop the whole window

    return segments, None


@router.get(
    "/streams/{camera_id}/recordings",
    response_model=RecordingsResponse,
    summary="Recorded segments available for a camera in a time window",
)
async def stream_recordings(
    camera_id: uuid.UUID,
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
    start: Annotated[datetime, Query(description="UTC window start")],
    end: Annotated[datetime, Query(description="UTC window end")],
) -> RecordingsResponse:
    """List what has been recorded for this camera between `start` and `end`.

    Carries no detection data - see `GET /api/v1/detections` for the
    department-scoped, already-built read path for that; the console calls
    both and merges them for the playback timeline.
    """
    exists = await connection.fetchval("SELECT 1 FROM cameras WHERE id = $1", camera_id)
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"camera '{camera_id}' is not registered",
        )
    if end <= start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="end must be after start",
        )

    path_name = f"cam-{camera_id}"
    segments, note = await _list_recorded_segments(path_name, start, end, settings)
    return RecordingsResponse(camera_id=camera_id, path=path_name, segments=segments, note=note)


@router.get(
    "/streams/{camera_id}/clip",
    summary="A recorded clip for a camera, as MP4",
)
async def stream_clip(
    camera_id: uuid.UUID,
    connection: Annotated[asyncpg.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
    start: Annotated[datetime, Query(description="UTC clip start")],
    duration_seconds: Annotated[float, Query(gt=0, le=3600, description="Clip length")] = 30.0,
) -> StreamingResponse:
    """Proxy MediaMTX's playback `/get` for one camera's recorded window.

    Streamed rather than buffered, so a multi-minute clip does not sit in this
    process's memory. Same discovery caveat as `_list_recorded_segments`: the
    exact query shape is provisional until confirmed against the running
    playback server.
    """
    exists = await connection.fetchval("SELECT 1 FROM cameras WHERE id = $1", camera_id)
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"camera '{camera_id}' is not registered",
        )

    base = (settings.mediamtx_playback_base_url or "").rstrip("/")
    if not base:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="recording playback is not configured (MEDIAMTX_PLAYBACK_BASE_URL unset)",
        )

    path_name = f"cam-{camera_id}"
    client = await _get_media_client(settings)
    try:
        upstream = client.build_request(
            "GET",
            f"{base}/get",
            params={
                "path": path_name,
                "start": start.astimezone(timezone.utc).isoformat(),
                "duration": duration_seconds,
            },
        )
        response = await client.send(upstream, stream=True)
    except httpx.HTTPError as exc:
        raise _media_unreachable(settings, exc) from exc

    if response.status_code != status.HTTP_200_OK:
        await response.aclose()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"no recorded clip for camera '{camera_id}' at {start.isoformat()} "
                f"+{duration_seconds:g}s"
            ),
        )

    return StreamingResponse(
        response.aiter_bytes(),
        media_type=response.headers.get("content-type", "video/mp4"),
        background=response.aclose,
    )


# ---------------------------------------------------------------------------
# Recording reconciliation
# ---------------------------------------------------------------------------

# camera_id -> (stream_url, retention_days) last successfully applied. Lets
# the reconciler skip cameras whose config has not changed, rather than
# re-POSTing identical config to MediaMTX every tick forever - PATCHing an
# unchanged path is at best wasted work and at worst (unconfirmed either way)
# risks a needless source restart, which would show up as exactly the kind of
# dropout this feature exists to capture.
_recording_state: dict[uuid.UUID, tuple[str, int]] = {}
_recording_task: asyncio.Task[None] | None = None


async def _reconcile_recording_paths(settings: Settings) -> None:
    """Ensure every ACTIVE, recording-enabled camera has a continuous path.

    Recording needs the source running whether or not anyone is watching, so
    this - not a viewer connecting - is what brings each camera's pull up.
    `recording_enabled` is `NOT NULL DEFAULT false` on `cameras`, so a camera
    only joins the recording set once that column is explicitly turned on -
    see the one-time backfill this feature shipped with, which sets it for
    every existing row.

    Published-local paths (test videos already streaming to this MediaMTX
    instance under their own path name) are out of scope: they do not use the
    `cam-<uuid>` naming this function assumes, and none of the real fleet uses
    them.
    """
    pool = get_pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT id, stream_url, retention_days
            FROM cameras
            WHERE status = 'ACTIVE' AND recording_enabled
            ORDER BY global_camera_code
            """
        )

    seen: set[uuid.UUID] = set()
    for row in rows:
        camera_id = row["id"]
        raw_stream_url = row["stream_url"]
        if _is_mediamtx_source(raw_stream_url, settings):
            continue  # published-local test video; not this reconciler's job

        seen.add(camera_id)
        stream_url = _with_grid_credentials(raw_stream_url, settings)
        retention_days = row["retention_days"] or settings.default_retention_days

        if _recording_state.get(camera_id) == (stream_url, retention_days):
            continue  # unchanged since last tick; nothing to push

        try:
            await _ensure_recording_path(camera_id, stream_url, retention_days, settings)
            _recording_state[camera_id] = (stream_url, retention_days)
        except Exception:
            logger.exception(
                "failed to ensure recording path", extra={"camera_id": str(camera_id)}
            )
            continue

        logger.info(
            "recording path established",
            extra={"camera_id": str(camera_id), "retention_days": retention_days},
        )
        # Only actual new dials are staggered - a camera whose config was
        # already current above never reaches this sleep. Recording needs
        # every camera connected at once, not just the one being watched;
        # dialling all of them in the same instant is simultaneous connections
        # against an external grid that has already shown auth failures under
        # far lighter load.
        await asyncio.sleep(settings.recording_stagger_seconds)

    # Cameras that left the recording set (deactivated, or recording_enabled
    # flipped off) are simply forgotten here - their already-written segments
    # are left alone to expire on their own recordDeleteAfter. Nothing deletes
    # the MediaMTX path itself; a future viewer would just re-create it
    # on-demand through the normal _ensure_pull_path branch.
    for camera_id in list(_recording_state):
        if camera_id not in seen:
            del _recording_state[camera_id]


async def _recording_reconcile_loop(settings: Settings) -> None:
    while True:
        try:
            await _reconcile_recording_paths(settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("recording reconciliation iteration failed")
        await asyncio.sleep(settings.recording_reconcile_interval_seconds)


def start_recording_reconciler(settings: Settings) -> None:
    """Start the recording reconciler, unless MediaMTX is not the backend."""
    global _recording_task

    if settings.media_backend != "mediamtx":
        logger.info("recording reconciler not started: media backend is not mediamtx")
        return
    if _recording_task is not None and not _recording_task.done():
        return

    _recording_task = asyncio.create_task(
        _recording_reconcile_loop(settings), name="recording-reconciler"
    )
    logger.info(
        "recording reconciler started",
        extra={"interval_seconds": settings.recording_reconcile_interval_seconds},
    )


async def stop_recording_reconciler() -> None:
    global _recording_task

    if _recording_task is not None:
        task, _recording_task = _recording_task, None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Session maintenance
# ---------------------------------------------------------------------------


async def _maintain_mediamtx_sessions(settings: Settings) -> None:
    """Retire aged sessions and free their WHEP resources.

    WHEP has no heartbeat: a session lives until it is DELETEd or its ICE
    connection drops. MediaMTX already frees sessions when the browser goes
    away, so this is cleanup for rows whose browser vanished without a clean
    teardown - and it stops `relay_sessions` growing without bound.
    """
    pool = get_pool()
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            """
            UPDATE relay_sessions
            SET state = 'CLOSED', closed_at = clock_timestamp()
            WHERE state = 'ACTIVE'
              AND started_at < clock_timestamp() - ($1 * INTERVAL '1 second')
            RETURNING session_id, relay_addr
            """,
            settings.relay_session_max_age_seconds,
        )

    if not rows:
        return

    client = await _get_media_client(settings)
    for row in rows:
        resource = row["relay_addr"]
        if not resource or "/whep" not in resource:
            continue
        try:
            await client.delete(resource)
        except httpx.HTTPError as exc:
            # Best-effort: MediaMTX has very likely already reclaimed it.
            logger.debug(
                "whep resource delete failed",
                extra={"session_id": row["session_id"], "error": str(exc)},
            )


async def _heartbeat_relay_sessions(settings: Settings) -> None:
    """Heartbeat every ACTIVE legacy relay session and retire the dead ones.

    Only the Go relay needs this: it reaps a session after 30s of silence and
    the console never calls its heartbeat route.
    """
    pool = get_pool()

    async with pool.acquire() as connection:
        await connection.execute(
            """
            UPDATE relay_sessions
            SET state = 'CLOSED', closed_at = clock_timestamp()
            WHERE state = 'ACTIVE'
              AND started_at < clock_timestamp() - ($1 * INTERVAL '1 second')
            """,
            settings.relay_session_max_age_seconds,
        )
        rows = await connection.fetch(
            "SELECT session_id FROM relay_sessions WHERE state = 'ACTIVE'"
        )

    if not rows:
        return

    client = await _get_relay_client(settings)
    for row in rows:
        session_id = row["session_id"]
        try:
            response = await client.post(f"/api/v1/streams/heartbeat/{session_id}")
        except httpx.HTTPError as exc:
            logger.warning(
                "session heartbeat failed",
                extra={"session_id": session_id, "error": str(exc)},
            )
            continue

        async with pool.acquire() as connection:
            if response.status_code == status.HTTP_200_OK:
                await connection.execute(
                    """
                    UPDATE relay_sessions
                    SET last_heartbeat_at = clock_timestamp()
                    WHERE session_id = $1
                    """,
                    session_id,
                )
            elif response.status_code == status.HTTP_404_NOT_FOUND:
                # The relay already reaped or restarted; stop paying for this row.
                await connection.execute(
                    """
                    UPDATE relay_sessions
                    SET state = 'CLOSED', closed_at = clock_timestamp()
                    WHERE session_id = $1
                    """,
                    session_id,
                )
                logger.info(
                    "session no longer known to relay", extra={"session_id": session_id}
                )


async def _keepalive_loop(settings: Settings) -> None:
    backend = settings.media_backend
    while True:
        try:
            if backend == "mediamtx":
                await _maintain_mediamtx_sessions(settings)
            elif backend == "relay":
                await _heartbeat_relay_sessions(settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("session maintenance iteration failed")
        await asyncio.sleep(settings.relay_session_keepalive_seconds)


def start_keepalive(settings: Settings) -> None:
    """Start session maintenance, unless no media backend is configured."""
    global _keepalive_task

    backend = settings.media_backend
    if backend == "none":
        logger.info("session maintenance not started: no media backend configured")
        return
    if _keepalive_task is not None and not _keepalive_task.done():
        return

    _keepalive_task = asyncio.create_task(
        _keepalive_loop(settings), name="stream-session-maintenance"
    )
    logger.info(
        "session maintenance started",
        extra={
            "backend": backend,
            "interval_seconds": settings.relay_session_keepalive_seconds,
        },
    )


async def stop_keepalive() -> None:
    """Cancel maintenance and release both backend clients."""
    global _keepalive_task

    if _keepalive_task is not None:
        task, _keepalive_task = _keepalive_task, None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    await aclose_clients()
