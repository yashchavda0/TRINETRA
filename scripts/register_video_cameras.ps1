<#
.SYNOPSIS
    Registers one camera per published test video in the Model 1 registry.

.DESCRIPTION
    Reads the paths MediaMTX currently knows about and POSTs a camera row for
    each, with stream_url pointing back at that MediaMTX path. The API resolves
    stream_url server-side when the console asks for live video, so this is what
    connects a marker on the map to a playing stream.

    Cameras are placed on a ring around the given centre so the markers do not
    stack on top of each other, each facing the centre.

    Idempotent: a camera whose global_camera_code already exists is reported and
    skipped, so re-running after adding a video is safe.

.PARAMETER ApiBaseUrl
    Registry API. Defaults to http://127.0.0.1:8000.

.PARAMETER CentreLat / CentreLon
    Centre of the ring. Defaults to Ahmedabad (23.0225, 72.5714).

.PARAMETER RadiusMeters
    Ring radius. Default 700 m - inside the 1500 m graph edge threshold, so the
    cameras form a connected handoff graph.

.EXAMPLE
    .\scripts\register_video_cameras.ps1
#>
[CmdletBinding()]
param(
    [string]$ApiBaseUrl = "http://127.0.0.1:8000",
    [string]$MediaMtxApi = "http://127.0.0.1:9997",
    [double]$CentreLat = 23.0225,
    [double]$CentreLon = 72.5714,
    [double]$RadiusMeters = 700,
    [string]$DepartmentId = "POLICE",
    [string]$RtspHost = "127.0.0.1:8554"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

# --- discover published paths ---------------------------------------------

try {
    $paths = Invoke-RestMethod "$MediaMtxApi/v3/paths/list" -TimeoutSec 5
}
catch {
    throw "MediaMTX control API unreachable at $MediaMtxApi. Start it with: docker compose -f docker-compose.infra.yml up -d"
}

$ready = @($paths.items | Where-Object { $_.ready })
if ($ready.Count -eq 0) {
    Write-Host "No streams are publishing yet. Run .\scripts\publish_videos.ps1 first." -ForegroundColor Yellow
    exit 0
}

try {
    Invoke-RestMethod "$ApiBaseUrl/health" -TimeoutSec 5 | Out-Null
}
catch {
    throw "Registry API unreachable at $ApiBaseUrl. Start it with: uvicorn app.main:app --port 8000"
}

Write-Host "Registering $($ready.Count) camera(s) from published streams`n" -ForegroundColor Cyan

# --- place on a ring -------------------------------------------------------

# Metres per degree at this latitude. Longitude degrees shrink by cos(lat);
# ignoring that would squash the ring into an ellipse.
$metresPerDegLat = 111320.0
$metresPerDegLon = 111320.0 * [Math]::Cos($CentreLat * [Math]::PI / 180.0)

$registered = @()
$index = 0

foreach ($path in $ready) {
    $slug = $path.name
    $angle = (2.0 * [Math]::PI * $index) / $ready.Count
    $index++

    $lat = $CentreLat + ($RadiusMeters * [Math]::Cos($angle)) / $metresPerDegLat
    $lon = $CentreLon + ($RadiusMeters * [Math]::Sin($angle)) / $metresPerDegLon

    # Face the centre: bearing from the camera back to the middle of the ring.
    $azimuth = [Math]::Round((($angle * 180.0 / [Math]::PI) + 180.0) % 360.0, 2)

    $code = "VID-" + ($slug.ToUpperInvariant() -replace '[^A-Z0-9]+', '-')
    $body = @{
        global_camera_code = $code
        department_id      = $DepartmentId
        latitude           = [Math]::Round($lat, 6)
        longitude          = [Math]::Round($lon, 6)
        azimuth_angle      = $azimuth
        fov_degrees        = 70.0
        stream_url         = "rtsp://$RtspHost/$slug"
        vms_vendor         = "MediaMTX"
        status             = "ACTIVE"
    } | ConvertTo-Json -Compress

    try {
        $camera = Invoke-RestMethod -Method Post -Uri "$ApiBaseUrl/api/v1/cameras" `
            -ContentType "application/json" -Body $body -TimeoutSec 10
        Write-Host ("  {0,-28} {1}  ({2}, {3})" -f $code, $camera.id, $camera.latitude, $camera.longitude) -ForegroundColor Green
        $registered += [pscustomobject]@{ Code = $code; Id = $camera.id; Path = $slug }
    }
    catch {
        $statusCode = $_.Exception.Response.StatusCode.value__
        if ($statusCode -eq 409) {
            Write-Host ("  {0,-28} already registered - skipped" -f $code) -ForegroundColor Yellow
        }
        else {
            Write-Host ("  {0,-28} FAILED ({1})" -f $code, $statusCode) -ForegroundColor Red
        }
    }
}

if ($registered.Count -gt 0) {
    Write-Host "`nCamera UUIDs (use with scripts\publish_test_event.py):" -ForegroundColor Cyan
    $registered | Format-Table -AutoSize | Out-String | Write-Host
}

Write-Host "Open the console at http://localhost:5173, click a VID- marker, then Request Live Stream." -ForegroundColor Cyan
