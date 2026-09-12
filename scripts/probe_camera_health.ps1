<#
.SYNOPSIS
    Checks whether each registered camera actually streams, and records the result.

.DESCRIPTION
    Nothing else in the platform writes camera_health_logs, so every camera's
    popup reads "unknown" until this runs. The probe is a real one: it asks the
    local MediaMTX to pull the camera's own stream_url and waits for the source
    to become ready, which is exactly what the console's live-stream request
    does. A camera that answers here will show video when clicked.

    For each camera:
      1. a temporary MediaMTX path is created pointing at the camera's URL,
         with sourceOnDemand disabled so the dial happens immediately;
      2. readiness is polled until -TimeoutSeconds;
      3. the path is deleted again, so no grid connection is left open - the
         grid asks clients not to hold streams they are not watching;
      4. the outcome is POSTed to /api/v1/cameras/health-ping as one batch.

    Grid credentials are read from .env and attached only to hosts on the grid
    allowlist (LIVE_GRID_MEDIA_HOST and the host of LIVE_GRID_HLS_BASE_URL) -
    the same rule app/routers/streams.py applies, so no third-party camera URL
    can be handed our password. The credentials are never written to the
    registry and never printed.

.PARAMETER ApiBaseUrl
    Registry API. Defaults to http://127.0.0.1:8000.

.PARAMETER MediaMtxApiUrl
    MediaMTX control API. Defaults to MEDIAMTX_API_BASE_URL in .env, else
    http://127.0.0.1:9997.

.PARAMETER CodeFilter
    Only probe cameras whose global_camera_code matches this wildcard, e.g.
    GRID-*. Default: every ACTIVE camera.

.PARAMETER TimeoutSeconds
    How long one camera gets to produce a stream. Default 15.

.PARAMETER Loop
    Keep probing every -IntervalSeconds instead of exiting after one pass.

.PARAMETER IntervalSeconds
    Gap between passes when -Loop is given. Default 300.

.EXAMPLE
    .\scripts\probe_camera_health.ps1 -CodeFilter GRID-*

.EXAMPLE
    .\scripts\probe_camera_health.ps1 -Loop -IntervalSeconds 600
#>
[CmdletBinding()]
param(
    [string]$ApiBaseUrl = "http://127.0.0.1:8000",
    [string]$ApiKey,
    [string]$MediaMtxApiUrl,
    [string]$CodeFilter = "*",
    [int]$TimeoutSeconds = 15,
    [switch]$Loop,
    [int]$IntervalSeconds = 300
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

# --- settings: process environment, then .env ------------------------------

$dotenv = @{}
if (Test-Path ".env") {
    foreach ($line in Get-Content ".env") {
        if ($line -match '^\s*(LIVE_GRID_[A-Z0-9_]+|MEDIAMTX_[A-Z0-9_]+|TRINETRA_API_KEY)\s*=\s*(.*?)\s*$') {
            $dotenv[$Matches[1]] = $Matches[2].Trim('"').Trim("'")
        }
    }
}

function Get-Setting {
    param([string]$Name, [string]$Fallback = "")

    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) { $value = $dotenv[$Name] }
    if ([string]::IsNullOrWhiteSpace($value)) { return $Fallback }
    return $value
}

if (-not $PSBoundParameters.ContainsKey("MediaMtxApiUrl")) {
    $MediaMtxApiUrl = (Get-Setting "MEDIAMTX_API_BASE_URL" "http://127.0.0.1:9997").TrimEnd('/')
}

$gridEmail = Get-Setting "LIVE_GRID_EMAIL"
$gridPassword = Get-Setting "LIVE_GRID_PASSWORD"

# The registry authenticates every call. An unattended script is not a person,
# so it carries a service key rather than a login - see scripts/create_api_key.ps1.
if (-not $PSBoundParameters.ContainsKey("ApiKey")) { $ApiKey = Get-Setting "TRINETRA_API_KEY" }
if ([string]::IsNullOrWhiteSpace($ApiKey)) {
    throw "No API key. The registry rejects unauthenticated calls. Mint one with .\scripts\create_api_key.ps1 -Name grid-tooling, put it in .env as TRINETRA_API_KEY, or pass -ApiKey."
}
$authHeaders = @{ "X-API-Key" = $ApiKey }

# Hosts allowed to receive the grid credentials. Mirrors
# Settings.live_grid_media_hosts - an allowlist, not a convenience: without it
# any camera URL someone registered would be dialled with our password.
$allowedHosts = New-Object System.Collections.Generic.HashSet[string]
$mediaHost = Get-Setting "LIVE_GRID_MEDIA_HOST"
if ($mediaHost) {
    $bare = ($mediaHost -split '//')[-1].Split('/')[0]
    if ($bare -match '^(?<host>[^:]+):\d+$') { $bare = $Matches["host"] }
    if ($bare) { [void]$allowedHosts.Add($bare.ToLowerInvariant()) }
}
$hlsBase = Get-Setting "LIVE_GRID_HLS_BASE_URL"
if ($hlsBase) {
    try { [void]$allowedHosts.Add(([Uri]$hlsBase).Host.ToLowerInvariant()) } catch { }
}

function Add-GridCredentials {
    param([string]$Url)

    if (-not ($gridEmail -and $gridPassword)) { return $Url }
    $uri = [Uri]$Url
    if (-not $allowedHosts.Contains($uri.Host.ToLowerInvariant())) { return $Url }
    # An explicit userinfo in the row is an operator's deliberate override.
    if ($uri.UserInfo) { return $Url }

    $user = [Uri]::EscapeDataString($gridEmail)
    $pass = [Uri]::EscapeDataString($gridPassword)
    $port = if ($uri.IsDefaultPort) { "" } else { ":$($uri.Port)" }
    return "$($uri.Scheme)://${user}:${pass}@$($uri.Host)$port$($uri.PathAndQuery)"
}

# --- one probe -------------------------------------------------------------

function Test-CameraStream {
    param([string]$CameraId, [string]$StreamUrl)

    $path = "probe-$CameraId"
    $body = @{
        source         = (Add-GridCredentials $StreamUrl)
        sourceOnDemand = $false
        sourceProtocol = "tcp"
    } | ConvertTo-Json -Compress

    $started = Get-Date
    $ready = $false
    $tracks = @()
    $note = ""

    try {
        try {
            Invoke-RestMethod -Method Post -Uri "$MediaMtxApiUrl/v3/config/paths/add/$path" `
                -ContentType "application/json" -Body $body -TimeoutSec 10 | Out-Null
        }
        catch {
            # MediaMTX 1.9.x registers the patch route as PATCH only; a leftover
            # path from an interrupted run lands here.
            Invoke-RestMethod -Method Patch -Uri "$MediaMtxApiUrl/v3/config/paths/patch/$path" `
                -ContentType "application/json" -Body $body -TimeoutSec 10 | Out-Null
        }

        $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 500
            try {
                $info = Invoke-RestMethod "$MediaMtxApiUrl/v3/paths/get/$path" -TimeoutSec 10
            }
            catch { continue }
            if ($info.ready) {
                $ready = $true
                $tracks = @($info.tracks)
                break
            }
        }
        if (-not $ready) { $note = "no stream within ${TimeoutSeconds}s" }
    }
    catch {
        $note = $_.Exception.Message
    }
    finally {
        # Always tear the path down: leaving it would hold a grid connection
        # open with nobody watching.
        try {
            Invoke-RestMethod -Method Delete -Uri "$MediaMtxApiUrl/v3/config/paths/delete/$path" -TimeoutSec 10 | Out-Null
        }
        catch { }
    }

    $latency = [int][Math]::Round(((Get-Date) - $started).TotalMilliseconds)
    return [pscustomobject]@{
        Ready     = $ready
        LatencyMs = $latency
        Tracks    = ($tracks -join ',')
        Note      = $note
    }
}

# --- one pass over the fleet ----------------------------------------------

function Invoke-ProbePass {
    try {
        $registry = Invoke-RestMethod "$ApiBaseUrl/api/v1/cameras?limit=10000" -Headers $authHeaders -TimeoutSec 15
    }
    catch {
        if ($_.Exception.Response.StatusCode.value__ -in 401, 403) {
            throw "Registry rejected the API key (HTTP $($_.Exception.Response.StatusCode.value__)). Mint a fresh one with .\scripts\create_api_key.ps1 and update TRINETRA_API_KEY in .env."
        }
        throw "Registry API unreachable at $ApiBaseUrl. Start it with: uvicorn app.main:app --port 8000"
    }

    $cameras = @($registry.items |
            Where-Object { $_.status -eq "ACTIVE" -and $_.global_camera_code -like $CodeFilter })

    if ($cameras.Count -eq 0) {
        Write-Host "No ACTIVE cameras match '$CodeFilter'." -ForegroundColor Yellow
        return
    }

    try {
        Invoke-RestMethod "$MediaMtxApiUrl/v3/paths/list" -TimeoutSec 10 | Out-Null
    }
    catch {
        throw "MediaMTX control API unreachable at $MediaMtxApiUrl. Start it with: docker compose -f docker-compose.infra.yml up -d mediamtx"
    }

    Write-Host "Probing $($cameras.Count) camera(s) via $MediaMtxApiUrl`n" -ForegroundColor Cyan

    $pings = @()
    $up = 0
    foreach ($camera in $cameras) {
        $result = Test-CameraStream -CameraId $camera.id -StreamUrl $camera.stream_url
        if ($result.Ready) {
            $up++
            Write-Host ("  {0,-14} UP    {1,6} ms  {2}" -f $camera.global_camera_code, $result.LatencyMs, $result.Tracks) -ForegroundColor Green
            # Latency here is time-to-first-frame, not a network round trip: it
            # includes the dial, the RTSP handshake and the wait for a keyframe.
            $pings += @{ camera_id = $camera.id; ping_latency_ms = $result.LatencyMs; is_reachable = $true }
        }
        else {
            Write-Host ("  {0,-14} DOWN  {1}" -f $camera.global_camera_code, $result.Note) -ForegroundColor Red
            # The API rejects a latency alongside is_reachable=false, and rightly:
            # a failed probe has no round-trip time to report.
            $pings += @{ camera_id = $camera.id; is_reachable = $false }
        }
    }

    $body = @{ pings = $pings } | ConvertTo-Json -Depth 4 -Compress
    try {
        $result = Invoke-RestMethod -Method Post -Uri "$ApiBaseUrl/api/v1/cameras/health-ping" `
            -Headers $authHeaders -ContentType "application/json" -Body $body -TimeoutSec 30
        Write-Host "`n$up/$($cameras.Count) reachable. $($result.inserted) health row(s) recorded." -ForegroundColor Cyan
    }
    catch {
        Write-Host "`nProbe finished but recording failed: $($_.Exception.Message)" -ForegroundColor Red
    }
}

# --- run -------------------------------------------------------------------

if (-not $Loop) {
    Invoke-ProbePass
    Write-Host "Camera popups now read UP/DOWN instead of unknown. Re-run to refresh." -ForegroundColor DarkGray
    exit 0
}

Write-Host "Looping every ${IntervalSeconds}s. Ctrl+C to stop." -ForegroundColor Cyan
while ($true) {
    Invoke-ProbePass
    Start-Sleep -Seconds $IntervalSeconds
}
