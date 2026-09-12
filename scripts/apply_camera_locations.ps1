<#
.SYNOPSIS
    Moves registered grid cameras to their surveyed positions.

.DESCRIPTION
    The external live-feed grid's catalogue publishes an id and a name and
    nothing else, so register_grid_cameras.ps1 has no coordinates to work with
    and places every camera on a ring around Ahmedabad. This script applies the
    positions in scripts/grid_camera_locations.json instead, by PATCHing each
    camera row through the registry API.

    Re-registering would not work: the row's UUID is already referenced by
    detections, alerts and relay sessions, and there is no delete endpoint.

    Idempotent. A camera already within -ToleranceMeters of its target is
    reported as unchanged and not written, so a re-run after editing one entry
    touches only that entry.

    Nothing here is a survey. Every position was read off the camera's name -
    see the "precision" field on each entry, and correct anything wrong by
    editing the JSON and running this again.

.PARAMETER LocationsFile
    Defaults to scripts/grid_camera_locations.json.

.PARAMETER ApiBaseUrl
    Registry API. Defaults to http://127.0.0.1:8000.

.PARAMETER CodePrefix
    How a catalogue id maps to a global_camera_code. Defaults to GRID-, which
    is what register_grid_cameras.ps1 writes.

.PARAMETER ToleranceMeters
    A camera closer than this to its target is left alone. Default 25 m.

.PARAMETER List
    Print what would change, then exit without writing.

.EXAMPLE
    .\scripts\apply_camera_locations.ps1 -List

.EXAMPLE
    .\scripts\apply_camera_locations.ps1
#>
[CmdletBinding()]
param(
    [string]$LocationsFile,
    [string]$ApiBaseUrl = "http://127.0.0.1:8000",
    [string]$ApiKey,
    [string]$CodePrefix = "GRID-",
    [double]$ToleranceMeters = 25,
    [switch]$List
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

# The registry authenticates every call; an unattended script carries a service
# key rather than a login. Flag wins, then the process environment, then .env.
if (-not $PSBoundParameters.ContainsKey("ApiKey")) {
    $ApiKey = [Environment]::GetEnvironmentVariable("TRINETRA_API_KEY")
    if ([string]::IsNullOrWhiteSpace($ApiKey) -and (Test-Path ".env")) {
        foreach ($line in Get-Content ".env") {
            if ($line -match '^\s*TRINETRA_API_KEY\s*=\s*(.*?)\s*$') {
                $ApiKey = $Matches[1].Trim('"').Trim("'")
                break
            }
        }
    }
}
if ([string]::IsNullOrWhiteSpace($ApiKey)) {
    throw "No API key. The registry rejects unauthenticated calls. Mint one with .\scripts\create_api_key.ps1 -Name grid-tooling, put it in .env as TRINETRA_API_KEY, or pass -ApiKey."
}
$authHeaders = @{ "X-API-Key" = $ApiKey }

if ([string]::IsNullOrWhiteSpace($LocationsFile)) {
    $LocationsFile = Join-Path $PSScriptRoot "grid_camera_locations.json"
}
if (-not (Test-Path $LocationsFile)) {
    throw "Locations file not found: $LocationsFile"
}

$document = Get-Content $LocationsFile -Raw | ConvertFrom-Json
$wanted = @($document.cameras)
if ($wanted.Count -eq 0) { throw "$LocationsFile lists no cameras." }

# Metres between two WGS84 points, good enough at city scale to decide whether a
# camera has already been moved.
function Get-DistanceMeters {
    param([double]$Lat1, [double]$Lon1, [double]$Lat2, [double]$Lon2)

    $metresPerDegLat = 111320.0
    $metresPerDegLon = 111320.0 * [Math]::Cos($Lat1 * [Math]::PI / 180.0)
    $dLat = ($Lat2 - $Lat1) * $metresPerDegLat
    $dLon = ($Lon2 - $Lon1) * $metresPerDegLon
    return [Math]::Sqrt($dLat * $dLat + $dLon * $dLon)
}

# --- read the registry -----------------------------------------------------

try {
    $registry = Invoke-RestMethod "$ApiBaseUrl/api/v1/cameras?limit=10000" -Headers $authHeaders -TimeoutSec 15
}
catch {
    if ($_.Exception.Response.StatusCode.value__ -in 401, 403) {
        throw "Registry rejected the API key (HTTP $($_.Exception.Response.StatusCode.value__)). Mint a fresh one with .\scripts\create_api_key.ps1 and update TRINETRA_API_KEY in .env."
    }
    throw "Registry API unreachable at $ApiBaseUrl. Start it with: uvicorn app.main:app --port 8000"
}

$byCode = @{}
foreach ($camera in $registry.items) { $byCode[$camera.global_camera_code] = $camera }

# --- resolve every move before writing anything ----------------------------

$moves = @()
$unchanged = @()
$missing = @()

foreach ($entry in $wanted) {
    # Same normalisation register_grid_cameras.ps1 uses to build the code.
    $code = $CodePrefix + (("$($entry.id)").ToUpperInvariant() -replace '[^A-Z0-9]+', '-')
    $camera = $byCode[$code]
    if ($null -eq $camera) {
        $missing += [pscustomobject]@{ Code = $code; Reason = "not registered - run register_grid_cameras.ps1 first" }
        continue
    }

    $distance = Get-DistanceMeters -Lat1 $camera.latitude -Lon1 $camera.longitude `
        -Lat2 $entry.latitude -Lon2 $entry.longitude

    $row = [pscustomobject]@{
        Code      = $code
        Id        = $camera.id
        Name      = $entry.name
        Precision = $entry.precision
        FromLat   = [Math]::Round($camera.latitude, 6)
        FromLon   = [Math]::Round($camera.longitude, 6)
        ToLat     = [Math]::Round([double]$entry.latitude, 6)
        ToLon     = [Math]::Round([double]$entry.longitude, 6)
        MoveKm    = [Math]::Round($distance / 1000.0, 2)
    }

    if ($distance -le $ToleranceMeters) { $unchanged += $row } else { $moves += $row }
}

# --- preview ---------------------------------------------------------------

if ($moves.Count -gt 0) {
    $moves | Select-Object Code, Precision, ToLat, ToLon, MoveKm, Name |
        Format-Table -AutoSize | Out-String | Write-Host
}

Write-Host "$($moves.Count) to move, $($unchanged.Count) already in place, $($missing.Count) missing." -ForegroundColor Cyan

$lowConfidence = @($moves | Where-Object { $_.Precision -ne "junction" })
if ($lowConfidence.Count -gt 0) {
    Write-Host "$($lowConfidence.Count) position(s) are town-level or a guess, read off the camera name rather than surveyed. Correct them in $LocationsFile." -ForegroundColor Yellow
}
if ($missing.Count -gt 0) {
    $missing | Format-Table -AutoSize | Out-String | Write-Host
}

if ($List) {
    Write-Host "-List: nothing was changed." -ForegroundColor Cyan
    exit 0
}
if ($moves.Count -eq 0) { exit 0 }

# --- apply -----------------------------------------------------------------

Write-Host "`nUpdating $($moves.Count) camera(s)`n" -ForegroundColor Cyan

$failed = 0
foreach ($move in $moves) {
    # Latitude and longitude must go together - the API rejects a half-position
    # rather than store a point at (0, lat).
    $body = @{ latitude = $move.ToLat; longitude = $move.ToLon } | ConvertTo-Json -Compress
    try {
        $updated = Invoke-RestMethod -Method Patch -Uri "$ApiBaseUrl/api/v1/cameras/$($move.Id)" `
            -Headers $authHeaders -ContentType "application/json" -Body $body -TimeoutSec 10
        Write-Host ("  {0,-12} -> ({1}, {2})  {3}" -f $move.Code, $updated.latitude, $updated.longitude, $move.Name) -ForegroundColor Green
    }
    catch {
        $failed++
        $statusCode = $_.Exception.Response.StatusCode.value__
        Write-Host ("  {0,-12} FAILED ({1}) {2}" -f $move.Code, $statusCode, $_.Exception.Message) -ForegroundColor Red
    }
}

if ($failed -gt 0) {
    Write-Host "`n$failed update(s) failed." -ForegroundColor Red
    exit 1
}

Write-Host "`nReload the console and press Fit cameras - the fleet now spans Gujarat, not one ring." -ForegroundColor Cyan
Write-Host "Handoff note: cameras further apart than EDGE_DISTANCE_THRESHOLD_M (default 1500 m) share no graph edge, so the engine will see mostly isolated nodes." -ForegroundColor DarkGray
