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
| Ingestion | `cmd/ingestion_gateway` | mTLS protobuf intake, publishes to Kafka | needs Go + MinGW |
| Streaming | `cmd/stream_relay` | mTLS RTSP→WebRTC session control, spawns ffmpeg | needs Go + ffmpeg |
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
| Go 1.22+ | `cmd/*` | `winget install GoLang.Go` |
| ffmpeg | `cmd/stream_relay` transcoding | `winget install Gyan.FFmpeg` |
| MinGW-w64 gcc | `cmd/ingestion_gateway` (cgo librdkafka) | `winget install BrechtSanders.WinLibs.POSIX.UCRT` |

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

| What | Where |
|---|---|
| Registry API | <http://127.0.0.1:8000> |
| API docs (Swagger) | <http://127.0.0.1:8000/docs> |
| GIS console | <http://localhost:5173> |

The Go tier (ingestion gateway, stream relay) is **not** part of this sequence — it needs Go, ffmpeg
and MinGW installed first. See [Go tier](#go-tier-ingestion--streaming) below.

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
winget install BrechtSanders.WinLibs.POSIX.UCRT        # cgo librdkafka for the gateway
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
npm run dev
```

Port: **5173** (strict — it will not silently pick another).
**Up when:** the map renders and the alert badge is not stuck reconnecting.

Vite proxies `/api/v1`, `/api/v2`, `/alerts` (WebSocket) and `/health` to `127.0.0.1:8000`, so the
console needs no configuration. Point it at a different API with `$env:VITE_API_TARGET`.

Production build: `npm run build` then `npm run preview` (port 4173).

### Go tier: ingestion + streaming

Requires Go, ffmpeg, MinGW and `.\scripts\gen_dev_certs.ps1` from First-time setup. Both binaries
require mTLS material and exit immediately without it.

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
| `/health` answers but names a different service | another process already holds port 8000 | see the port check below, then run on `--port 8010` and set `ALERT_PUBLISH_URL` / `P0_ALERT_WS_URL` to match |
| `SettingsError: error parsing value for field "cors_origins"` | list-typed settings are JSON-decoded by pydantic-settings | `CORS_ORIGINS` is comma-separated — do not wrap it in `[...]` |
| Migration aborts on `NOTICE: extension "postgis" already exists` | PowerShell turns native stderr into a terminating error | handled by `Invoke-Native` in `scripts/migrate.ps1`; just re-run it |
| Worker: `UNKNOWN_TOPIC_OR_PART` | topic not created yet | expected before the first publish; auto-created on first produce |
| Worker: `camera graph loaded: 0 nodes` | no cameras with `status='ACTIVE'` | register cameras first — see [Smoke test](#smoke-test) |
| Worker: `generated/surveillance_event_pb2.py is missing` | codegen never run | `.\scripts\gen_proto.ps1` |
| API startup: `camera graph load failed: cannot connect` | Postgres down or wrong `DATABASE_URL` | check `docker compose ... ps` and the `DATABASE_URL` line in `.env` |
| Typing a script name opens Notepad | you are in cmd.exe, which cannot execute `.ps1` | use the `.cmd` wrapper: `scripts\publish_videos.cmd -List` |
| Stream request returns **503** | no media backend configured at all | set `MEDIAMTX_WHEP_BASE_URL` (default already points at the container) and restart the API |
| Stream request returns **502** "media server is unreachable" | the mediamtx container is down | `docker compose -f docker-compose.infra.yml up -d mediamtx` |
| Stream request returns **504** "camera source … did not start streaming" | the media server is fine; the **camera's** RTSP endpoint is not answering | check the camera is reachable and its `stream_url` is right. The smoke-test cameras (`AHM-TRF-…`) have invented URLs and will always fail this way — only `VID-` cameras from `register_video_cameras` can stream |
| Stream request returns **409** "no live stream on media path" | path exists, nothing publishing to it | `.\scripts\publish_videos.ps1` (check with `-List`) |
| Console: stream request returns 503 mentioning `STREAM_RELAY_BASE_URL` | you are running an API process started before the MediaMTX work landed | restart `uvicorn` — Python loads modules once at startup |
| Console: alert badge stuck on `reconnecting` | API down, or `/alerts` proxy not reached | confirm the API is up; the console must use `alertsWsUrl="/alerts/p0"`, not the component's `ws://central-command` default |

Check what owns a port:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen |
  ForEach-Object { "PID $($_.OwningProcess) $((Get-Process -Id $_.OwningProcess).ProcessName)" }
```

With everything running, verify the pipeline end to end with the [Smoke test](#smoke-test) below.

---

## Live video testing

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

Each layer can be checked on its own, which makes a failure easy to localise:

| Layer | Check | Proves |
|---|---|---|
| Ingest | `Invoke-RestMethod http://127.0.0.1:9997/v3/paths/list` | the publisher is pushing (`ready: true`, `bytesReceived` climbing) |
| RTSP | open `rtsp://127.0.0.1:8554/<slug>` in VLC | the stream is valid, independent of WebRTC |
| WebRTC | open `http://127.0.0.1:8889/<slug>` | ICE, DTLS, SRTP and the UDP port publishing all work |
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
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/cameras/$($camera.id)/health"     # UNKNOWN until polled

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
