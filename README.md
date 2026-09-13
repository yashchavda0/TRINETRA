# TRINETRA — Gujarat Police CCTV Integration System

Integration platform for a multi-department CCTV fleet (target scale 80,000+ cameras across Police,
RTO, GSRTC, Civil Supplies, Revenue and private operators).

| Tier | Path | What it is | Runs natively on Windows? |
|---|---|---|---|
| Contract | `proto/surveillance_event.proto` | Protobuf v3 detection event, the analytics bus wire format | n/a |
| Schema | `sql/001…`, `sql/002…` | PostgreSQL 16 + PostGIS 3.4 migrations | in Docker |
| Media | `mediamtx.yml` | MediaMTX — RTSP ingest, WebRTC delivery. Carries all live video | in Docker |
| Model 1 | `app/` | FastAPI camera registry, WebRTC signalling proxy, P0 alert fan-out | yes |
| Model 4 | `engine/` + `workers/handoff_worker.py` | Re-ID matcher, STGCN predictive handoff, wake-up state machine | yes (CPU) |
| Integrations | `adapters/` | eGujCop/CCTNS and VAHAN lookups, threat alert construction | yes |
| Ingestion | `cmd/ingestion_gateway` | mTLS protobuf intake, publishes to Kafka | yes (Go or Docker) |
| Streaming | `cmd/stream_relay` | mTLS RTSP→WebRTC session control, spawns ffmpeg | yes (Go or Docker) |
| Console | `frontend/` | React + OpenLayers GIS console | yes |

**Invariants across every tier:** WGS84 / EPSG:4326 for all spatial data (longitude is X — PostGIS
takes `ST_MakePoint(longitude, latitude)`), and UTC Unix epoch **milliseconds** (`int64`) for every
timestamp crossing a wire.

---

## Prerequisites

| Tool | Needed for | Install |
|---|---|---|
| Docker Desktop | Postgres/PostGIS + Kafka | already installed here — **start it**, the daemon is what serves `docker` |
| Python 3.11+ | API, worker, adapters | present (3.13) |
| Node 18+ | console | present (24.x) |
| Go 1.22+ | `cmd/*` | `winget install GoLang.Go` (pure Go — CGo/MinGW no longer needed) |
| ffmpeg | `cmd/stream_relay` transcoding | `winget install Gyan.FFmpeg` |

`psql` and `protoc` are **not** required: migrations run inside the Postgres container, and protobuf
codegen goes through `grpcio-tools` from pip.

### Which shell

Every script ships twice, so it runs from either prompt with the same arguments:

| Shell | How to run | Note |
|---|---|---|
| PowerShell | `.\scripts\publish_videos.ps1 -List` | what the examples below use |
| cmd.exe | `scripts\publish_videos.cmd -List` | the `.cmd` re-launches the `.ps1` |

**If typing a script name opens Notepad, you are in cmd.exe.** cmd does not execute `.ps1` files — it
hands them to their file association. Use the `.cmd` form. The wrappers also pass
`-ExecutionPolicy Bypass`, so they work on a machine with a restricted execution policy.

---

## Quick start

Everything already installed? This is the whole sequence. Run it from the repository root, in the
order shown. Docker Desktop must be running first — the `docker` CLI works without it, but every
command fails with `failed to connect to the docker API`.

If you created a virtualenv during First-time setup, activate it in every Python shell below:
`.\.venv\Scripts\Activate.ps1`.

```powershell
# --- shell 1: infrastructure, then migrations -------------------------------
# Needs docker/postgres.env to exist (First-time setup step 4) or compose aborts.
docker compose -f docker-compose.infra.yml up -d
docker compose -f docker-compose.infra.yml ps          # both must read (healthy)
.\scripts\migrate.ps1                                  # idempotent, safe to re-run

# --- shell 2: registry API (leave running) ----------------------------------
uvicorn app.main:app --port 8000

# --- shell 3: Model 4 worker (leave running) --------------------------------
python -m workers.handoff_worker

# --- shell 4: GIS console (leave running) -----------------------------------
cd frontend; npm run dev
```

For live pictures you also need cameras with working stream URLs. Fastest path, if you have the
external live-feed grid: fill the `LIVE_GRID_*` block in `.env`, then
`.\scripts\register_grid_cameras.ps1` — no video files, no ffmpeg containers. See
[Live video testing](#live-video-testing).

| What | Where |
|---|---|
| Registry API | <http://127.0.0.1:8000> |
| API docs (Swagger) | <http://127.0.0.1:8000/docs> |
| GIS console | <http://localhost:5173> |

The Go tier (ingestion gateway, stream relay) is **not** part of this sequence — it can run natively via Go or in Docker containers. See [Go tier](#go-tier-ingestion--streaming) below.

---

## First-time setup

Run these once. Everything after this is the Quick start above.

```powershell
# 1. Python environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1                           # re-run in every new shell

# 2. torch first, from the CPU index: it is far smaller than the default CUDA
#    wheels, and torch-geometric requires torch to already be importable.
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt

# 3. Protobuf bindings (build artifact, not committed)
.\scripts\gen_proto.ps1                                # -> generated/surveillance_event_pb2.py

# 4. Configuration. Both files are gitignored; the compose stack will not start
#    without docker/postgres.env. If you change POSTGRES_USER/PASSWORD/DB there,
#    update DATABASE_URL in .env to match.
Copy-Item .env.example .env
Copy-Item docker/postgres.env.example docker/postgres.env

# 5. Console dependencies
cd frontend; npm install; cd ..
```

For the Go tier only, additionally:

```powershell
winget install GoLang.Go                               # cmd/*
winget install Gyan.FFmpeg                             # stream relay transcoding
# open a NEW shell so PATH picks them up, then:
go mod tidy                                            # writes go.sum — commit it
go build ./...
.\scripts\gen_dev_certs.ps1                            # development PKI in certs/
```

---

## Start each tier

### Infrastructure — PostgreSQL/PostGIS + Kafka + MediaMTX

```powershell
docker compose -f docker-compose.infra.yml up -d
docker compose -f docker-compose.infra.yml ps --format "{{.Name}} {{.Status}}"
```

Ports, all bound to `127.0.0.1` only: **5432** (Postgres), **9092** (Kafka),
**8554** (RTSP ingest), **8889** (WebRTC signalling), **8189/udp** (WebRTC media), **9997**
(MediaMTX control API).
**Up when:** all three containers report `(healthy)`. Kafka takes ~30 s on a cold start.

Always address Kafka as `127.0.0.1:9092`, never `localhost:9092` — see
[Troubleshooting](#troubleshooting).

### Migrations

```powershell
.\scripts\migrate.ps1
```

Applies `sql/001_init_model1_registry.sql` then `sql/002_pipeline_state.sql` via psql inside the
container, so no local PostgreSQL client is needed. Both are transactional and idempotent.
**Up when:** the script prints the table list and `3.4 USE_GEOS=1 USE_PROJ=1 USE_STATS=1`.

Confirm the nine application tables:

```powershell
docker compose -f docker-compose.infra.yml exec -T postgres `
  psql -U trinetra -d trinetra -c "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename;"
```

### Registry API

```powershell
uvicorn app.main:app --port 8000
```

Port: **8000**. Add `--reload` while developing.
**Up when:**

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health | ConvertTo-Json -Compress
# {"status":"ok","service":"Model 1 - Central GIS Camera Registry","database":"up",...}
```

`database: "up"` is the part that matters — `"degraded"` means the API is running but Postgres is not
reachable. Stop with `Ctrl+C`. **Any `.env` change requires a restart to take effect.**

### Model 4 worker

```powershell
python -m workers.handoff_worker
```

No port; it consumes Kafka and writes to Postgres.
**Up when:** the log shows `"message": "worker started"` with `device`, `topic` and `graph_nodes`.

`UNKNOWN_TOPIC_OR_PART` before the first event is published is expected — the topic is auto-created on
first produce. `camera graph loaded: 0 nodes` means no ACTIVE cameras are registered yet.

### GIS console

```powershell
cd frontend
copy .env.example .env   # once: carries the alert-channel token
npm run dev
```

Port: **5173** (strict — it will not silently pick another).
**Up when:** the map renders, the HUD reads `Cameras <n>` with n > 0, and the P0 badge says `live`.

Vite proxies `/api/v1`, `/api/v2`, `/alerts` (WebSocket) and `/health` to `127.0.0.1:8000`. Point it
at a different API with `$env:VITE_API_TARGET`.

`frontend/.env` needs **one** value: `VITE_P0_ALERT_TOKEN`, equal to `P0_ALERT_API_KEY` in the
repository-root `.env`. A browser cannot set headers on a WebSocket, so the console presents the key
as `?token=` on `/alerts/p0`; without it the API closes the handshake and the browser reports a bare
`403`. Vite compiles every `VITE_`-prefixed value into the bundle, so treat this as a development
key only.

The HUD shows what the console actually loaded — camera count, how many rows had no coordinates, and
the alert-channel state — so an empty map says why it is empty. **Fit cameras** zooms to the
registered cameras when the markers are off-screen.

Production build: `npm run build` then `npm run preview` (port 4173).

### Go tier: ingestion + streaming

Requires certificates from `.\scripts\gen_dev_certs.ps1`. Both services require mTLS material and exit immediately without it.

#### Option A: Native execution (Requires Go 1.22+ on host)

```powershell
# --- ingestion gateway ---
$env:INGEST_LISTEN_ADDR=":8443"
$env:INGEST_TLS_CERT_FILE="certs/ingest-server.crt"
$env:INGEST_TLS_KEY_FILE="certs/ingest-server.key"
$env:INGEST_TLS_CA_FILE="certs/ca.crt"
$env:KAFKA_BOOTSTRAP_SERVERS="127.0.0.1:9092"
go run ./cmd/ingestion_gateway                         # https://127.0.0.1:8443

# --- stream relay (separate shell) ---
$env:STREAM_RELAY_LISTEN_ADDR=":9443"
$env:STREAM_RELAY_TLS_CERT_FILE="certs/relay-server.crt"
$env:STREAM_RELAY_TLS_KEY_FILE="certs/relay-server.key"
$env:STREAM_RELAY_TLS_CA_FILE="certs/ca.crt"
go run ./cmd/stream_relay                              # https://127.0.0.1:9443
```

#### Option B: Docker multi-stage build (No local Go required)

Using multi-stage Docker builds with Alpine, binaries are statically compiled inside containers without needing Go or MinGW installed on your host system:

```powershell
# Build slim Docker images (~20MB and ~50MB)
docker build -f docker/Dockerfile.ingestion_gateway -t trinetra-ingestion-gateway .
docker build -f docker/Dockerfile.stream_relay -t trinetra-stream-relay .
```

Ports: **8443** (gateway, single route `POST /api/v1/events/ingest`), **9443** (relay).
Neither exposes a health endpoint; a clean startup log is the up-check.

Then enable live streaming in the console:

```powershell
# set in .env, then RESTART the API
STREAM_RELAY_BASE_URL=https://127.0.0.1:9443
```

Until that is set, `POST /api/v2/webrtc/offer` returns a deliberate 503 and everything else works.
Note limitation 1 below: signalling will succeed and **no media will arrive**.

---

## Stop, reset, restart

```powershell
# stop containers, keep all data
docker compose -f docker-compose.infra.yml down

# recreate one container after editing docker-compose.infra.yml
docker compose -f docker-compose.infra.yml up -d --force-recreate kafka

# stop the API and worker started above
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like "*workers.handoff_worker*" -or $_.CommandLine -like "*uvicorn app.main:app*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

# tail a background service's log while it runs
docker compose -f docker-compose.infra.yml logs -f kafka
```

**Destructive — full reset.** `down -v` deletes the named volumes: every registered camera,
detection, embedding, alert and the entire Kafka log are permanently gone. There is no backup.

```powershell
docker compose -f docker-compose.infra.yml down -v
docker compose -f docker-compose.infra.yml up -d
.\scripts\migrate.ps1
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `failed to connect to the docker API` / `Docker Desktop is unable to start` | daemon down, CLI present | start Docker Desktop, wait for the whale icon to settle, re-run `docker compose -f docker-compose.infra.yml ps` |
| Producer logs `Connect to ipv6#[::1]:9092 failed`, then messages are dropped | `localhost` resolves to `::1` first on Windows; the published port is IPv4-only | use `127.0.0.1:9092` everywhere — never `localhost:9092` |
| `/health` answers but names a different service | another process already holds port 8000 | find it with `Get-NetTCPConnection -State Listen -LocalPort 8000 \| Select-Object OwningProcess` and stop it (`Stop-Process -Id <pid>`), then start uvicorn on 8000. Every URL in this project - `ALERT_PUBLISH_URL`, `P0_ALERT_WS_URL`, `DETECTION_PUBLISH_URL`, the Vite proxy, every script under `scripts/` - is hardcoded to port 8000; running a second instance on another port to work around a stuck one just leaves two copies of the same service alive and out of sync, not two services |
| `SettingsError: error parsing value for field "cors_origins"` | list-typed settings are JSON-decoded by pydantic-settings | `CORS_ORIGINS` is comma-separated — do not wrap it in `[...]` |
| Migration aborts on `NOTICE: extension "postgis" already exists` | PowerShell turns native stderr into a terminating error | handled by `Invoke-Native` in `scripts/migrate.ps1`; just re-run it |
| Worker: `UNKNOWN_TOPIC_OR_PART` | topic not created yet | expected before the first publish; auto-created on first produce |
| Worker: `camera graph loaded: 0 nodes` | no cameras with `status='ACTIVE'` | register cameras first — see [Smoke test](#smoke-test) |
| Worker: `generated/surveillance_event_pb2.py is missing` | codegen never run | `.\scripts\gen_proto.ps1` |
| API startup: `camera graph load failed: cannot connect` | Postgres down or wrong `DATABASE_URL` | check `docker compose ... ps` and the `DATABASE_URL` line in `.env` |
| Typing a script name opens Notepad | you are in cmd.exe, which cannot execute `.ps1` | use the `.cmd` wrapper: `scripts\publish_videos.cmd -List` |
| Stream request returns **503** | no media backend configured at all | set `MEDIAMTX_WHEP_BASE_URL` (default already points at the container) and restart the API |
| Stream request returns **502** "media server is unreachable" | the mediamtx container is down | `docker compose -f docker-compose.infra.yml up -d mediamtx` |
| Stream request returns **504** "camera source … did not start streaming" | the media server is fine; the **camera's** RTSP endpoint is not answering | check the camera is reachable and its `stream_url` is right. The smoke-test cameras (`AHM-TRF-…`) have invented URLs and will always fail this way — only `VID-` and `GRID-` cameras can stream |
| **504** on a `GRID-` camera, and `ffprobe` needs credentials to play it | `LIVE_GRID_EMAIL`/`LIVE_GRID_PASSWORD` unset, wrong, or the email is not on the approved list | fill both in `.env`, then **restart `uvicorn`** — the API reads them at startup and injects them when MediaMTX dials |
| **504** on a `GRID-` camera that `ffprobe` plays fine from Windows | MediaMTX dials the camera from **inside its container**, where the grid host does not resolve | check `8554/TCP` outbound is open; if the grid is tunnelled, set `LIVE_GRID_MEDIA_HOST` to the tunnel's host:port (not `-RtspHostRewrite`, which puts the host off the credential allowlist) and re-register |
| `register_grid_cameras.ps1` says "Catalogue unreadable" | `LIVE_GRID_CATALOGUE_URL` host does not resolve, or every authentication attempt was refused | the note lists what each attempt returned. The run still registers from `LIVE_GRID_CAMERA_IDS`; fix the URL or credentials if the id list is stale |
| Catalogue note reads "returned no camera list" for every attempt | the grid answers an unauthenticated catalogue request with its sign-in page and HTTP **200**, not a 401 | the script posts `LIVE_GRID_EMAIL`/`LIVE_GRID_PASSWORD` to `<catalogue-host>/auth/login` and retries with the session cookie. If that also fails, the email is probably not on the approved list |
| `GRID-` camera connects but the tile stays black | H.265 — MediaMTX does not transcode for WebRTC and desktop Chrome mostly will not decode it | check the codec column from `register_grid_cameras.ps1 -List`; transcode to H.264 into our MediaMTX if you must preview it |
| `register_grid_cameras.ps1` refuses with "this stack's own MediaMTX address" | the grid is reached on `127.0.0.1:8554`, which `_is_mediamtx_source` claims as ours | use the grid's real host, or forward it to another port and pass `-RtspHostRewrite` |
| Stream request returns **409** "no live stream on media path" | path exists, nothing publishing to it | `.\scripts\publish_videos.ps1` (check with `-List`) |
| Console: stream request returns 503 mentioning `STREAM_RELAY_BASE_URL` | you are running an API process started before the MediaMTX work landed | restart `uvicorn` — Python loads modules once at startup |
| Console: `WebSocket … /alerts/p0 failed … Unexpected response code: 403` | `P0_ALERT_API_KEY` is set server-side and the console presented no token | create `frontend/.env` from `frontend/.env.example` with `VITE_P0_ALERT_TOKEN` equal to `P0_ALERT_API_KEY`, then restart `npm run dev`. The badge now reads `unauthorised` instead of looping |
| Console: alert badge stuck on `reconnecting` | API down, or `/alerts` proxy not reached | confirm the API is up; the console must use `alertsWsUrl="/alerts/p0"`, not the component's `ws://central-command` default |
| Console: HUD reads `Cameras 0` but `/api/v1/cameras` returns rows | rows have no usable coordinates, or the fetch failed | the HUD says which: `registry unreachable` for a failed fetch, `(n without coordinates)` for undrawable rows |
| Cameras are all stacked in one ring around Ahmedabad | the grid catalogue carries no coordinates, so registration synthesized positions | `.\scripts\apply_camera_locations.ps1` — see [Positions](#positions) |
| Camera popup status reads `unknown` | nothing has ever written `camera_health_logs` for that camera | `.\scripts\probe_camera_health.ps1 -CodeFilter GRID-*` |
| `PATCH /api/v1/cameras/{id}` from a browser is blocked by CORS | deliberate: `allow_methods` is `GET, POST, OPTIONS` (`app/main.py`) | it is an operator/script action — run it from PowerShell, or widen `allow_methods` if a console ever needs to edit rows |

Check what owns a port:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen |
  ForEach-Object { "PID $($_.OwningProcess) $((Get-Process -Id $_.OwningProcess).ProcessName)" }
```

With everything running, verify the pipeline end to end with the [Smoke test](#smoke-test) below.

---

## Live video testing

Two sources, same path through the platform. If you have access to the **external live-feed grid**,
use it — it is real live video with no local publishing at all. Otherwise fall back to local video
files.

### Option A — the external live-feed grid

Every camera is a live stream: one second of video takes one second to arrive, frames carry
monotonic presentation timestamps, and there is no seeking and no running ahead of real time. Treat
each endpoint as a physical camera.

The access model is split across two hosts, because a CDN cannot proxy RTSP or WebRTC:

| Protocol | Endpoint | Reachable via | Intended for |
|---|---|---|---|
| HLS | `https://<cdn-host>/<id>/index.m3u8` | public, password | dashboards, mobile, restricted networks, remote AI |
| RTSP | `rtsp://<email>:<password>@<public-ip>:8554/stream/<id>` | public IP, direct | AI inference (OpenCV, GStreamer, FFmpeg, DeepStream) |
| WebRTC (WHEP) | `http://<email>:<password>@<public-ip>:8889/stream/<id>/whep` | public IP, direct | low-latency browser preview |

RTSP and WHEP need the gateway ports open on your network: **8554/TCP, 8889/TCP, 8189/UDP**.

**Everything the grid needs lives in `.env` under `LIVE_GRID_*`** — the current values there are
test placeholders, so replace the two hosts when the real grid is issued. Nothing else in the tree
hardcodes a grid host, port or camera id.

```powershell
# 1. Fill in .env: the two hosts, and your registered email + access password.
#    Do NOT percent-encode the email's @ here - the API does that.
#    LIVE_GRID_CATALOGUE_URL   catalogue of cameras (source of truth)
#    LIVE_GRID_HLS_BASE_URL    CDN base for HLS
#    LIVE_GRID_MEDIA_HOST      public IP serving RTSP/WebRTC
#    LIVE_GRID_EMAIL           an approved email, or nothing connects
#    LIVE_GRID_PASSWORD        access password
#    LIVE_GRID_CAMERA_IDS      fallback ids, only if the catalogue is unreadable

# 2. See what would be registered - writes nothing
.\scripts\register_grid_cameras.ps1 -List

# 3. Register one camera per live stream
.\scripts\register_grid_cameras.ps1

# 4. Put the cameras where they actually are (see "Positions" below)
.\scripts\apply_camera_locations.ps1 -List
.\scripts\apply_camera_locations.ps1

# 5. Record which cameras really stream, so popups stop reading "unknown"
.\scripts\probe_camera_health.ps1 -CodeFilter GRID-*

# 6. Open the console, click a GRID- marker, press "Request Live Stream"
cd frontend; npm run dev
```

#### Positions

The catalogue publishes an **id and a name only** — no coordinates:

```json
[{"id":"cam01","name":"01 Chiman bhai Bridge"}, {"id":"cam02","name":"02 Janpath"}, ...]
```

So registration has nothing to place cameras by and drops them all on a ring around Ahmedabad, even
though the real fleet is spread across Gujarat. `scripts/grid_camera_locations.json` carries a
position per camera read off its name, and `apply_camera_locations.ps1` PATCHes each row to it.
Every entry declares its own `precision` — `junction`, `town` or `guess` — because **none of it is
surveyed**; correct any row in the JSON and re-run, it only writes what moved.

Re-registering is not the way to fix a position: the row's UUID is already referenced by detections,
alerts and relay sessions, and there is no delete endpoint. `PATCH /api/v1/cameras/{id}` exists for
exactly this.

Spreading the fleet across the state has one consequence worth knowing: cameras further apart than
`EDGE_DISTANCE_THRESHOLD_M` (1500 m) share no handoff-graph edge, so the Model 4 engine sees mostly
isolated nodes — only the Bilimora and Junagadh clusters connect.

#### Health

Nothing else in the platform writes `camera_health_logs`, which is why a freshly registered camera's
popup reads `unknown`. `probe_camera_health.ps1` is a real probe, not a ping: it has the local
MediaMTX pull each camera's own `stream_url`, waits for the source to become ready, deletes the path
again (the grid asks clients not to hold streams nobody is watching), and POSTs the batch to
`/api/v1/cameras/health-ping`. A camera that comes back `UP` there will show video when clicked.

It also prints each camera's codec, which is the fastest way to spot the H.265 ones whose WebRTC
preview may stay black. Add `-Loop -IntervalSeconds 600` to keep the fleet's health fresh.

Nothing is published locally: the camera row stores the grid's own URL, and the API creates an
**on-demand pull path** for it over RTSP/TCP when a viewer asks
(`app/routers/streams.py:_ensure_pull_path`). On-demand means MediaMTX only holds the grid stream
open while someone is watching, which is what the grid asks of its clients. Cameras the catalogue
gives no coordinates for are placed on a ring around Ahmedabad, as the video-file script does.

#### Credentials

The grid authenticates **every** RTSP and WebRTC connection with an approved email and the access
password embedded in the URL, `@` percent-encoded as `%40`.

Those two values live in `.env` and nowhere else. Registry rows are stored **without** credentials,
and `_with_grid_credentials` (`app/routers/streams.py`) attaches them only when MediaMTX is told to
dial the camera — so the password never reaches Postgres and never comes back out of
`GET /api/v1/cameras`. Two consequences worth knowing:

- Editing `LIVE_GRID_EMAIL` / `LIVE_GRID_PASSWORD` needs a **`uvicorn` restart**; the API reads them
  at startup.
- Credentials are attached only to hosts on the grid allowlist (`LIVE_GRID_MEDIA_HOST` and the host
  of `LIVE_GRID_HLS_BASE_URL`), so no third-party camera URL can ever receive them. This is why
  tunnelling the grid to a different host is done by **changing `LIVE_GRID_MEDIA_HOST`**, not by
  `-RtspHostRewrite` — a rewritten host is off the allowlist and would be dialled anonymously. The
  script warns when you do that.

#### If 8554/TCP is blocked

Register the HLS endpoint instead — it goes through the CDN and works on any network:

```powershell
.\scripts\register_grid_cameras.ps1 -Protocol hls
```

MediaMTX pulls HLS as happily as RTSP; latency is higher, which is the trade.

#### Other things to know

- **`cameras.json` is the source of truth** for which cameras exist, and the set can change. If it
  cannot be read (DNS, 401, unexpected shape), the script says so and falls back to
  `LIVE_GRID_CAMERA_IDS` (`cam01-cam30`) — still a real registration, just with unconfirmed ids.
- **Do not reach the grid on `127.0.0.1:8554`.** The grid serves RTSP on 8554 and so does our own
  MediaMTX, and `_is_mediamtx_source` treats anything on `MEDIAMTX_RTSP_HOST` as already published
  here — the API would look for a local path named `stream/<id>` instead of pulling from the grid.
  The script refuses this case rather than writing rows that can never stream.
- **H.265 cameras** register fine, but the browser preview may stay black: MediaMTX does not
  transcode for WebRTC and most desktop Chrome builds will not decode H.265 there. The script flags
  which cameras those are. To preview one anyway, transcode it to H.264 into our own MediaMTX with
  an ffmpeg container (the pattern in `scripts/publish_videos.ps1`) and register that local path.
- **Each client gets its own copy** of a stream. `sourceOnDemand` already closes the grid connection
  when the last viewer leaves; do not open cameras you are not watching.

#### Client rules the grid expects

These bind any code that consumes the feeds directly — relevant to the frame-consuming worker
described in [Known limitations](#known-limitations--read-before-demoing), not to the console:

- Force **RTSP over TCP**. UDP fails across NAT and firewalls, and partial delivery produces corrupt
  frames that look like model bugs. Our pull path already sets `sourceProtocol: "tcp"`.
- Never trust the reported frame rate (`CAP_PROP_FPS`). Drive every time-derived metric from
  timestamps.
- Drive timing from **PTS**, never frame arrival time. On connect the gateway replays a buffered
  group-of-pictures, so the first second or two arrives faster than real time — a tracker that
  timestamps on arrival computes impossible velocities after every reconnect.
- Do not assume a constant frame rate; tolerate inter-frame gaps without calling them a disconnect.
- Reconnect with exponential backoff, ~2 s rising to a ~30 s cap. Never tight-loop.
- Decoder warnings at join (`Could not find ref with POC`) are normal until the first IDR frame.
  Log them; do not abort.
- Each feed loops, so expect an abrupt scene cut. Background models, re-ID galleries and track ids
  must recover from a hard discontinuity.
- There is no file download. Build against a live capture from the start.

Detections are still synthetic — nothing in this repo decodes frames yet, so use
`scripts\publish_test_event.py` with the UUIDs the script prints. See
[Known limitations](#known-limitations).

### Option B — local video files

No cameras on the network yet? Use video files. Each becomes a looping RTSP stream that behaves like
a live camera, so the whole path — RTSP ingest → WebRTC → console — is exercised for real.

```powershell
# 1. Put video files in videos\  (.mp4 .mkv .mov .ts .avi .webm)
mkdir videos

# 2. Publish each one as a looping RTSP stream
.\scripts\publish_videos.ps1
.\scripts\publish_videos.ps1 -List                     # what is publishing, and is it ready

# 3. Register one camera per video, placed on a ring around Ahmedabad
.\scripts\register_video_cameras.ps1

# 4. Open the console, click a VID- marker, press "Request Live Stream"
cd frontend; npm run dev
```

ffmpeg runs **inside a container** (one per video, named `trinetra-pub-<slug>`), so it does not need
to be installed on Windows. Stop them all with `.\scripts\publish_videos.ps1 -Stop`.

### Localising a failure

Each layer can be checked on its own. `<path>` below is the video slug for Option B, or
`cam-<camera-uuid>` for a grid camera's on-demand pull path (it exists only once a viewer has asked
for it at least once).

| Layer | Check | Proves |
|---|---|---|
| Source | Option B: `Invoke-RestMethod http://127.0.0.1:9997/v3/paths/list`. Option A: `ffprobe -rtsp_transport tcp rtsp://<grid-host>:8554/stream/<id>` | media is arriving (`ready: true`, `bytesReceived` climbing) / the grid stream itself is valid |
| RTSP | open `rtsp://127.0.0.1:8554/<path>` in VLC | the stream is valid, independent of WebRTC |
| WebRTC | open `http://127.0.0.1:8889/<path>` | ICE, DTLS, SRTP and the UDP port publishing all work |
| Proxy | `POST /api/v2/webrtc/offer` returns 200 with an SDP answer | our signalling translation works |
| Console | click a marker → "Request Live Stream" | the whole path |

If the MediaMTX page at 8889 plays but the console does not, the problem is in the proxy or the
browser code. If 8889 does not play either, it is ICE — check that `webrtcAdditionalHosts` in
`mediamtx.yml` names an address your browser can reach and that `8189/udp` is published.

---

## Smoke test

With the API running:

```powershell
# register a camera
$body = @{
  global_camera_code = "AHM-TRF-00001"
  department_id      = "POLICE"
  latitude           = 23.0225
  longitude          = 72.5714
  azimuth_angle      = 90
  stream_url         = "rtsp://10.20.30.40:554/stream1"
  vms_vendor         = "Milestone"
} | ConvertTo-Json

$camera = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/cameras `
  -ContentType application/json -Body $body
$camera.id

# list, radius search, health
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/cameras?limit=10"
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/cameras/spatial-search?latitude=23.0225&longitude=72.5714&radius_meters=2000"
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/cameras/$($camera.id)/health"     # UNKNOWN until probed

# correct a position or state; only the supplied fields are written, and
# latitude/longitude must travel together
Invoke-RestMethod -Method Patch -Uri "http://127.0.0.1:8000/api/v1/cameras/$($camera.id)" `
  -ContentType application/json -Body '{"latitude":23.0099,"longitude":72.5606}'

# feed the bus (worker picks it up)
python scripts/publish_test_event.py --camera-id $camera.id --lat 23.0225 --lon 72.5714 --plate GJ01AB1234

# contract violation: must be rejected, not stored
python scripts/publish_test_event.py --camera-id $camera.id --lat 23.0225 --lon 72.5714 --embedding-dim 128
```

Inspect the results:

```powershell
docker compose -f docker-compose.infra.yml exec -T postgres psql -U trinetra -d trinetra -c `
  "SELECT event_id, target_id, embedding_accepted FROM detections ORDER BY ingested_at DESC LIMIT 5;"
docker compose -f docker-compose.infra.yml exec -T postgres psql -U trinetra -d trinetra -c `
  "SELECT target_id, array_length(embedding,1) AS dims, sighting_count FROM target_embeddings;"
docker compose -f docker-compose.infra.yml exec -T postgres psql -U trinetra -d trinetra -c `
  "SELECT alert_id, priority, classification, plate_number FROM threat_alerts ORDER BY received_at DESC LIMIT 5;"
```

A two-camera handoff (register a second camera ~700 m away first):

```powershell
python scripts/publish_test_event.py --camera-id $a --lat 23.0225 --lon 72.5714 --track-id T-1
python scripts/publish_test_event.py --camera-id $b --lat 23.0290 --lon 72.5760 --track-id T-1 --offset-seconds 12
# then watch the wake-up mirror
#   SELECT camera_id, state, target_id, handoff_probability FROM camera_wake_state;
```

Once detection history exists, replace the fleet-wide transit guess with measured medians:

```powershell
python -m workers.seed_transition_stats --days 7 --min-samples 3
```

---

## Known limitations — read before demoing

These are real and deliberately not papered over:

1. **`cmd/stream_relay` cannot carry media and is bypassed by default.** Live video works — through
   MediaMTX — but the Go relay's own media path does not, and setting `STREAM_RELAY_BASE_URL` while
   `MEDIAMTX_WHEP_BASE_URL` is empty gets you signalling with no picture. Why it cannot work:
   `buildSDPAnswer` string-joins 18 literal SDP lines and emits **one** m-line against the browser's
   **two** (video + audio), which `setRemoteDescription` rejects outright; there is no ICE agent, no
   DTLS handshake and no SRTP; the DTLS fingerprint has no corresponding private key anywhere in the
   process; the only ICE candidate advertised is a `127.0.0.1` port whose socket `freeUDPPort` closed
   before returning it; and ffmpeg writes plain RTP, not SRTP, to that dead port. Fixing it means a
   `pion/webrtc` rewrite — the mTLS, RTSP probe, session registry and shutdown code would survive it.
2. **STGCN handoff probabilities are not meaningful.** The model is constructed with random weights —
   no checkpoint, trainer or dataset exists in the repository. The wiring is real; the numbers are
   not. Its temporal history is also synthetic: `refresh_embeddings` tiles one static feature frame
   across the window, so the GRU sees a constant sequence.
3. **`HTTP 202 QUEUED` from the ingestion gateway is not a durability guarantee.** `Produce` only
   enqueues into librdkafka's in-memory buffer, and delivery failures are logged and discarded. There
   is no outbox.
4. **Department code domain mismatch.** The gateway keys Kafka messages by the *numeric* protobuf enum
   tag (`"1"`) while the registry's primary key is the *textual* code (`'POLICE'`). The worker maps
   between them; the gateway still publishes the wrong domain.
5. **Connection-pool shutdown race in both Go services.** `Close()` closes the idle-connection channel
   while in-flight goroutines may still `Release` into it — a send on a closed channel panics.
6. **Bearings in the camera graph are planar.** `engine/tracking_pipeline.py` calls `ST_Azimuth` on the
   geometry rather than the geography, so the directional term uses a lat/lon-degree azimuth, not a
   true compass bearing — inconsistent with the distance term beside it, which does cast to geography.
7. **Alert channel auth is a single shared key**, and it is empty by default. Fine locally, not a
   production authorisation model.
8. **`certs/` is development-only PKI** — unencrypted keys, CA private key stored beside the
   certificates it signed. Production material must come from the department PKI.

Email	Password	Role	Sees
admin@trinetra.local	TrinetraDev!2026	State Administrator	all 35 cameras, every screen
police.admin@trinetra.local	PoliceDev!2026	Department Admin (POLICE)	34 cameras
rto.admin@trinetra.local	RtoDev!2026	Department Admin (RTO)	1 camera
operator@trinetra.local	OperDev!2026	Operator (POLICE)	34 cameras, no admin screens


- Agentic Video understanding can be implemented in this and at which layer and how?
- Parallel to ANPR can we also detect the Vehicles and capture the metadata of thme continously. so we can get all the information of vehicle not only Number plate.