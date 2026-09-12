<#
.SYNOPSIS
    Registers one camera per live stream on the external live-feed grid.

.DESCRIPTION
    Reads the grid's camera catalogue and POSTs a camera row for each live
    stream, with stream_url pointing at that camera's endpoint on the grid. The
    API resolves stream_url server-side when the console asks for live video:
    because the URL is not our own MediaMTX, it creates an on-demand pull path
    over RTSP/TCP and proxies WHEP from it.

    Every host, port and id defaults to a LIVE_GRID_* value read from .env, so
    .env is the single place the grid is described and the flags below are for
    one-off overrides only.

    URLs are written WITHOUT credentials. The grid authenticates every RTSP and
    WebRTC connection, but the API injects the email and password from .env at
    dial time, so the access password never reaches Postgres and is never
    returned by GET /api/v1/cameras.

    The catalogue is the source of truth for which cameras exist. If it cannot
    be read, the script falls back to LIVE_GRID_CAMERA_IDS and says so - a
    fallback run is a real registration, not a dry run.

    Cameras the catalogue gives no coordinates for are placed on a ring around
    the given centre so the markers do not stack, each facing the centre.

    Idempotent: a camera whose global_camera_code already exists is reported and
    skipped, so re-running after the grid adds a camera is safe.

.PARAMETER CatalogueUrl
    Camera catalogue. Defaults to LIVE_GRID_CATALOGUE_URL.

.PARAMETER MediaHost
    Host serving RTSP and WebRTC directly (not the CDN). Defaults to
    LIVE_GRID_MEDIA_HOST.

.PARAMETER RtspPort
    Defaults to LIVE_GRID_RTSP_PORT, else 8554.

.PARAMETER HlsBaseUrl
    CDN base for HLS. Defaults to LIVE_GRID_HLS_BASE_URL.

.PARAMETER Protocol
    Which endpoint to register: rtsp (default) or hls. Use hls when 8554/TCP is
    blocked on this network - it reaches the grid through the CDN instead.

.PARAMETER CameraIds
    Fallback ids used only when the catalogue cannot be read. A range whose ends
    share a prefix (cam01-cam30) or a comma-separated list. Defaults to
    LIVE_GRID_CAMERA_IDS.

.PARAMETER ApiBaseUrl
    Registry API. Defaults to http://127.0.0.1:8000.

.PARAMETER CentreLat / CentreLon
    Centre of the fallback ring. Defaults to Ahmedabad (23.0225, 72.5714).

.PARAMETER RadiusMeters
    Ring radius. Default 700 m - inside the 1500 m graph edge threshold, so the
    cameras form a connected handoff graph.

.PARAMETER RtspHostRewrite
    host:port to substitute into every RTSP URL. MediaMTX dials the camera from
    inside its container, so the grid host has to be resolvable there - pass
    host.docker.internal:<port> when the grid is tunnelled to this Windows host.

.PARAMETER List
    Print the catalogue and what would be registered, then exit without writing.

.EXAMPLE
    .\scripts\register_grid_cameras.ps1 -List

.EXAMPLE
    .\scripts\register_grid_cameras.ps1

.EXAMPLE
    .\scripts\register_grid_cameras.ps1 -Protocol hls
#>
[CmdletBinding()]
param(
    [string]$CatalogueUrl,
    [string]$MediaHost,
    [int]$RtspPort,
    [string]$HlsBaseUrl,
    [ValidateSet("rtsp", "hls")]
    [string]$Protocol = "rtsp",
    [string]$CameraIds,
    [string]$ApiBaseUrl = "http://127.0.0.1:8000",
    [string]$ApiKey,
    [double]$CentreLat = 23.0225,
    [double]$CentreLon = 72.5714,
    [double]$RadiusMeters = 700,
    [string]$DepartmentId = "POLICE",
    [string]$RtspHostRewrite = "",
    [switch]$List
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

# --- settings: flag, then process environment, then .env -------------------

# The API reads .env through pydantic-settings; this script reads the same file
# so both halves of the system describe the grid from one place. Only LIVE_GRID_
# keys are taken, and the process environment still wins.
$dotenv = @{}
if (Test-Path ".env") {
    foreach ($line in Get-Content ".env") {
        if ($line -match '^\s*(LIVE_GRID_[A-Z0-9_]+|TRINETRA_API_KEY)\s*=\s*(.*?)\s*$') {
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

if (-not $PSBoundParameters.ContainsKey("CatalogueUrl")) { $CatalogueUrl = Get-Setting "LIVE_GRID_CATALOGUE_URL" }
if (-not $PSBoundParameters.ContainsKey("MediaHost")) { $MediaHost = Get-Setting "LIVE_GRID_MEDIA_HOST" }
if (-not $PSBoundParameters.ContainsKey("HlsBaseUrl")) { $HlsBaseUrl = Get-Setting "LIVE_GRID_HLS_BASE_URL" }
if (-not $PSBoundParameters.ContainsKey("CameraIds")) { $CameraIds = Get-Setting "LIVE_GRID_CAMERA_IDS" "cam01-cam30" }
if (-not $PSBoundParameters.ContainsKey("RtspPort")) {
    $RtspPort = [int](Get-Setting "LIVE_GRID_RTSP_PORT" "8554")
}

$gridEmail = Get-Setting "LIVE_GRID_EMAIL"
$gridPassword = Get-Setting "LIVE_GRID_PASSWORD"

# Registering a camera needs DEPT_ADMIN on the registry. An unattended script is
# not a person and must not hold someone's password, so it carries a service key
# instead - mint one with scripts/create_api_key.ps1.
if (-not $PSBoundParameters.ContainsKey("ApiKey")) { $ApiKey = Get-Setting "TRINETRA_API_KEY" }
if ([string]::IsNullOrWhiteSpace($ApiKey)) {
    throw "No API key. The registry rejects unauthenticated calls. Mint one with .\scripts\create_api_key.ps1 -Name grid-tooling, put it in .env as TRINETRA_API_KEY, or pass -ApiKey."
}
$authHeaders = @{ "X-API-Key" = $ApiKey }

# LIVE_GRID_MEDIA_HOST may carry its own port. Honour it, so a value like
# host.docker.internal:8554 does not become host:8554:8554 in the URL.
if ($MediaHost -match '^(?<host>[^:/]+):(?<port>\d+)$') {
    if (-not $PSBoundParameters.ContainsKey("RtspPort")) { $RtspPort = [int]$Matches["port"] }
    $MediaHost = $Matches["host"]
}

if ($Protocol -eq "rtsp" -and [string]::IsNullOrWhiteSpace($MediaHost)) {
    throw "No media host. Set LIVE_GRID_MEDIA_HOST in .env (see .env.example) or pass -MediaHost."
}
if ($Protocol -eq "hls" -and [string]::IsNullOrWhiteSpace($HlsBaseUrl)) {
    throw "No HLS base URL. Set LIVE_GRID_HLS_BASE_URL in .env (see .env.example) or pass -HlsBaseUrl."
}

# --- helpers ---------------------------------------------------------------

# The catalogue's key spellings are not guaranteed, so read each field by trying
# the plausible names in order rather than binding to one shape.
function Get-Field {
    param($Object, [string[]]$Names)

    if ($null -eq $Object) { return $null }
    foreach ($name in $Names) {
        $property = $Object.PSObject.Properties[$name]
        if ($null -ne $property -and $null -ne $property.Value -and "$($property.Value)" -ne "") {
            return $property.Value
        }
    }
    return $null
}

# First stream URL of the wanted scheme anywhere in a camera entry: a top-level
# key, or nested under a urls/streams object whose key names we do not want to
# depend on.
function Find-StreamUrl {
    param($Camera, [string]$SchemePattern)

    $direct = Get-Field $Camera @("rtsp_url", "rtsp", "rtspUrl", "hls_url", "hls", "stream_url", "url")
    if ($direct -is [string] -and $direct -match $SchemePattern) { return $direct }

    foreach ($containerName in @("urls", "stream_urls", "streams", "endpoints", "protocols")) {
        $container = Get-Field $Camera @($containerName)
        if ($null -eq $container) { continue }
        foreach ($property in $container.PSObject.Properties) {
            if ($property.Value -is [string] -and $property.Value -match $SchemePattern) {
                return $property.Value
            }
        }
    }
    return $null
}

function Test-IsLive {
    param($Camera)

    $value = Get-Field $Camera @("live", "is_live", "online", "status", "state")
    if ($null -eq $value) { return $true }   # catalogue is silent: assume usable
    if ($value -is [bool]) { return $value }
    return ("$value").ToLowerInvariant() -in @("true", "live", "online", "ready", "active", "up", "1")
}

function ConvertTo-Coordinate {
    param($Value)

    $parsed = 0.0
    if ($null -ne $Value -and [double]::TryParse(
            "$Value", [Globalization.NumberStyles]::Float,
            [Globalization.CultureInfo]::InvariantCulture, [ref]$parsed)) {
        return $parsed
    }
    return $null
}

# Credentials belong in .env and nowhere else, so anything the catalogue hands
# us with a password in it is stripped before it can reach the registry.
function Remove-UrlCredentials {
    param([string]$Url)

    $uri = [Uri]$Url
    if (-not $uri.UserInfo) { return $Url }
    $port = if ($uri.IsDefaultPort) { "" } else { ":$($uri.Port)" }
    return "$($uri.Scheme)://$($uri.Host)$port$($uri.PathAndQuery)"
}

# Replace only the host:port of a URL, preserving path and query.
function Set-UrlHost {
    param([string]$Url, [string]$HostPort)

    if ([string]::IsNullOrWhiteSpace($HostPort)) { return $Url }

    $uri = [Uri]$Url
    return "$($uri.Scheme)://$HostPort$($uri.PathAndQuery)"
}

function Expand-CameraIds {
    param([string]$Spec)

    $spec = $Spec.Trim()
    if ([string]::IsNullOrWhiteSpace($spec)) { return @() }
    if ($spec.Contains(",")) {
        return @($spec.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    }
    # A range whose two ends share a prefix and a zero-padded width, cam01-cam30.
    if ($spec -match '^(?<prefix>.*?)(?<start>\d+)\s*-\s*\k<prefix>(?<end>\d+)$') {
        $width = $Matches["start"].Length
        return @([int]$Matches["start"]..[int]$Matches["end"] | ForEach-Object {
                "$($Matches['prefix'])$($_.ToString().PadLeft($width, '0'))"
            })
    }
    return @($spec)
}

function New-StreamUrl {
    param([string]$Id)

    if ($Protocol -eq "hls") { return "$($HlsBaseUrl.TrimEnd('/'))/$Id/index.m3u8" }
    return "rtsp://${MediaHost}:${RtspPort}/stream/$Id"
}

# --- read the catalogue ----------------------------------------------------

$cameras = $null
$catalogueNote = ""

if ([string]::IsNullOrWhiteSpace($CatalogueUrl)) {
    $catalogueNote = "no LIVE_GRID_CATALOGUE_URL set"
}
else {
    # The catalogue's list lives either at the top level or one key down, and
    # the wrapper key is not guaranteed. Returns the array, or $null when the
    # body is not a camera list at all - which is how a sign-in page is
    # recognised without parsing HTML.
    function Get-CameraList {
        param($Body)

        if ($Body -is [Array]) { return @($Body) }
        foreach ($key in @("cameras", "items", "streams", "feeds", "data", "results")) {
            $candidate = Get-Field $Body @($key)
            if ($candidate -is [Array]) { return @($candidate) }
        }
        return $null
    }

    $attemptNotes = @()

    # 1. HTTP basic, when we have credentials. Some grids gate the catalogue
    #    this way, and it costs one request to find out.
    if ($gridEmail -and $gridPassword) {
        $securePassword = ConvertTo-SecureString $gridPassword -AsPlainText -Force
        try {
            $body = Invoke-RestMethod -Uri $CatalogueUrl -TimeoutSec 20 `
                -Credential ([PSCredential]::new($gridEmail, $securePassword))
            $cameras = Get-CameraList $body
            if ($null -eq $cameras) { $attemptNotes += "basic auth returned no camera list" }
        }
        catch {
            $attemptNotes += "basic auth - $($_.Exception.Message)"
        }
    }

    # 2. Unauthenticated: a 401 from sending the wrong kind of credential looks
    #    the same as a catalogue that wants none.
    if ($null -eq $cameras) {
        try {
            $body = Invoke-RestMethod -Uri $CatalogueUrl -TimeoutSec 20
            $cameras = Get-CameraList $body
            if ($null -eq $cameras) { $attemptNotes += "anonymous returned no camera list" }
        }
        catch {
            $attemptNotes += "anonymous - $($_.Exception.Message)"
        }
    }

    # 3. Session login. The grid's portal answers an unauthenticated catalogue
    #    request with its sign-in page and HTTP 200 - not a 401 - so neither
    #    attempt above can tell that it was refused, and both see a body that
    #    is simply not a camera list. Posting the same registered email and
    #    access password to the login form sets a session cookie, after which
    #    the catalogue is served as JSON on the same URL.
    if ($null -eq $cameras -and $gridEmail -and $gridPassword) {
        $catalogueUri = [Uri]$CatalogueUrl
        $loginUrl = "$($catalogueUri.Scheme)://$($catalogueUri.Authority)/auth/login"
        try {
            Invoke-WebRequest -Uri $loginUrl -Method Post -TimeoutSec 20 -UseBasicParsing `
                -Body @{ email = $gridEmail; password = $gridPassword } `
                -SessionVariable gridSession | Out-Null
            $body = Invoke-RestMethod -Uri $CatalogueUrl -TimeoutSec 20 -WebSession $gridSession
            $cameras = Get-CameraList $body
            if ($null -eq $cameras) { $attemptNotes += "session login returned no camera list" }
        }
        catch {
            $attemptNotes += "session login at $loginUrl - $($_.Exception.Message)"
        }
    }

    if ($null -eq $cameras -or $cameras.Count -eq 0) {
        $cameras = $null
        $catalogueNote = "$CatalogueUrl - $($attemptNotes -join '; ')"
    }
}

$fromCatalogue = $null -ne $cameras

if ($fromCatalogue) {
    Write-Host "Catalogue: $($cameras.Count) camera(s) from $CatalogueUrl`n" -ForegroundColor Cyan
}
else {
    # Everything downstream is identical either way, so a fallback run really
    # registers cameras - it just cannot promise the ids are current.
    $fallbackIds = Expand-CameraIds $CameraIds
    if ($fallbackIds.Count -eq 0) {
        throw "Catalogue unreadable ($catalogueNote) and no fallback ids. Set LIVE_GRID_CAMERA_IDS in .env or pass -CameraIds."
    }
    Write-Host "Catalogue unreadable: $catalogueNote" -ForegroundColor Yellow
    Write-Host "Falling back to LIVE_GRID_CAMERA_IDS ($CameraIds) - $($fallbackIds.Count) id(s), NOT confirmed by the grid.`n" -ForegroundColor Yellow
    $cameras = @($fallbackIds | ForEach-Object { [pscustomobject]@{ id = $_ } })
}

# --- resolve every camera before writing anything --------------------------

# Metres per degree at this latitude. Longitude degrees shrink by cos(lat);
# ignoring that would squash the ring into an ellipse.
$metresPerDegLat = 111320.0
$metresPerDegLon = 111320.0 * [Math]::Cos($CentreLat * [Math]::PI / 180.0)

$schemePattern = if ($Protocol -eq "hls") { '^https?://' } else { '^rtsps?://' }
$resolved = @()
$skipped = @()
$strippedCredentials = 0
$ringIndex = 0

foreach ($camera in $cameras) {
    $id = Get-Field $camera @("id", "camera_id", "stream_id", "name")
    if ($null -eq $id) {
        $skipped += [pscustomobject]@{ Code = "(no id)"; Reason = "catalogue entry has no id" }
        continue
    }

    if (-not (Test-IsLive $camera)) {
        $skipped += [pscustomobject]@{ Code = "$id"; Reason = "not live in the catalogue" }
        continue
    }

    $streamUrl = Find-StreamUrl $camera $schemePattern
    $inferred = $false
    if ($null -eq $streamUrl) {
        $streamUrl = New-StreamUrl "$id"
        $inferred = $true
    }
    else {
        $withoutCredentials = Remove-UrlCredentials $streamUrl
        if ($withoutCredentials -ne $streamUrl) {
            $strippedCredentials++
            $streamUrl = $withoutCredentials
        }
    }
    if ($Protocol -eq "rtsp") { $streamUrl = Set-UrlHost -Url $streamUrl -HostPort $RtspHostRewrite }

    $latitude = ConvertTo-Coordinate (Get-Field $camera @("latitude", "lat"))
    $longitude = ConvertTo-Coordinate (Get-Field $camera @("longitude", "lon", "lng"))
    if ($null -eq $latitude -or $null -eq $longitude) {
        $location = Get-Field $camera @("location", "position", "coordinates", "geo")
        if ($location -isnot [string]) {
            if ($null -eq $latitude) { $latitude = ConvertTo-Coordinate (Get-Field $location @("latitude", "lat")) }
            if ($null -eq $longitude) { $longitude = ConvertTo-Coordinate (Get-Field $location @("longitude", "lon", "lng")) }
        }
    }

    $azimuth = $null
    $placement = "catalogue"
    if ($null -eq $latitude -or $null -eq $longitude) {
        $placement = "ring"
        $angle = (2.0 * [Math]::PI * $ringIndex) / $cameras.Count
        $ringIndex++
        $latitude = $CentreLat + ($RadiusMeters * [Math]::Cos($angle)) / $metresPerDegLat
        $longitude = $CentreLon + ($RadiusMeters * [Math]::Sin($angle)) / $metresPerDegLon
        # Face the centre: bearing from the camera back to the middle of the ring.
        $azimuth = [Math]::Round((($angle * 180.0 / [Math]::PI) + 180.0) % 360.0, 2)
    }

    $codec = Get-Field $camera @("codec", "video_codec", "encoding")
    if ($null -eq $codec) { $codec = "unknown" }

    $resolved += [pscustomobject]@{
        Code      = "GRID-" + (("$id").ToUpperInvariant() -replace '[^A-Z0-9]+', '-')
        StreamUrl = $streamUrl
        Latitude  = [Math]::Round($latitude, 6)
        Longitude = [Math]::Round($longitude, 6)
        Azimuth   = $azimuth
        Codec     = "$codec"
        Placement = $placement
        Inferred  = $inferred
    }
}

if ($resolved.Count -eq 0) {
    Write-Host "No live cameras to register." -ForegroundColor Yellow
    $skipped | Format-Table -AutoSize | Out-String | Write-Host
    exit 0
}

# --- refuse the loopback collision -----------------------------------------

# app/routers/streams.py:_is_mediamtx_source treats any RTSP URL on
# MEDIAMTX_RTSP_HOST as already published on our own media server, and counts
# localhost / ::1 / mediamtx as the same host as 127.0.0.1. The grid also serves
# RTSP on 8554, so a tunnel onto 127.0.0.1:8554 makes the API hunt for a local
# path named stream/<id> instead of pulling from the grid. Fail loudly here
# rather than leaving a registry full of rows that can never stream.
$mediaMtxHost = Get-Setting "MEDIAMTX_RTSP_HOST" "127.0.0.1:8554"
$mediaMtxPort = if ($mediaMtxHost -match ':(\d+)$') { $Matches[1] } else { "8554" }
$loopback = @("127.0.0.1", "localhost", "::1", "mediamtx")

$collisions = @($resolved | Where-Object {
        $uri = [Uri]$_.StreamUrl
        $port = if ($uri.Port -gt 0) { "$($uri.Port)" } else { "554" }
        ($uri.Scheme -in @("rtsp", "rtsps")) -and
        ($uri.Host.ToLowerInvariant() -in $loopback) -and ($port -eq $mediaMtxPort)
    })

if ($collisions.Count -gt 0) {
    $offenders = ($collisions | ForEach-Object { "  $($_.Code)  $($_.StreamUrl)" }) -join "`n"
    throw @"
$($collisions.Count) of $($resolved.Count) camera URL(s) sit on this stack's own MediaMTX address
(MEDIAMTX_RTSP_HOST=$mediaMtxHost). The API would treat them as already published here and
never pull them from the grid:

$offenders

Fix one of these, then re-run:
  * reach the grid by its real IP or hostname instead of a loopback tunnel, or
  * forward the grid's 8554 to a different local port and pass that host:port
    to -RtspHostRewrite, or
  * change MEDIAMTX_RTSP_HOST (and the compose port binding) off $mediaMtxPort.
"@
}

# --- preview ---------------------------------------------------------------

$resolved |
    Select-Object Code, Codec, Placement,
        @{ Name = "Lat"; Expression = { $_.Latitude } },
        @{ Name = "Lon"; Expression = { $_.Longitude } },
        StreamUrl |
    Format-Table -AutoSize | Out-String | Write-Host

Write-Host "Protocol: $Protocol. URLs are stored without credentials - the API injects LIVE_GRID_EMAIL/LIVE_GRID_PASSWORD when MediaMTX dials the camera." -ForegroundColor DarkGray

if (-not ($gridEmail -and $gridPassword)) {
    Write-Host "LIVE_GRID_EMAIL / LIVE_GRID_PASSWORD are not both set. These rows will register, but every stream request will fail: the grid authenticates every connection." -ForegroundColor Yellow
}
if ($strippedCredentials -gt 0) {
    Write-Host "$strippedCredentials catalogue URL(s) carried credentials; stripped before storing." -ForegroundColor Yellow
}
if ($fromCatalogue -and ($resolved | Where-Object { $_.Inferred })) {
    Write-Host "Some URLs were not in the catalogue and were built from the documented $Protocol pattern." -ForegroundColor Yellow
}
if ($RtspHostRewrite -and $Protocol -eq "rtsp") {
    Write-Host "RTSP host rewritten to $RtspHostRewrite (MediaMTX dials the camera from inside its container)." -ForegroundColor Yellow
    # The API only attaches the grid credentials to a host on its own
    # allowlist, which is built from LIVE_GRID_MEDIA_HOST and
    # LIVE_GRID_HLS_BASE_URL. A rewritten host is not on it, so these rows
    # would be dialled anonymously and the grid would refuse them.
    $rewriteHost = ($RtspHostRewrite -split ':')[0]
    if ($rewriteHost -ne $MediaHost) {
        Write-Host "  Credentials will NOT be injected for $rewriteHost - it is not on the API's grid allowlist." -ForegroundColor Red
        Write-Host "  Set LIVE_GRID_MEDIA_HOST=$RtspHostRewrite in .env instead of using -RtspHostRewrite, then re-run." -ForegroundColor Red
    }
}

# MediaMTX does not transcode for WebRTC and most desktop Chrome builds will not
# decode H.265 there, so the tile can stay black on a perfectly healthy camera.
$h265 = @($resolved | Where-Object { $_.Codec -match '(?i)h\.?265|hevc' })
if ($h265.Count -gt 0) {
    Write-Host "$($h265.Count) camera(s) are H.265: registered, but the WebRTC preview may not render in the browser." -ForegroundColor Yellow
    Write-Host "  $(($h265.Code) -join ', ')" -ForegroundColor Yellow
}
if ($skipped.Count -gt 0) {
    Write-Host "Skipped $($skipped.Count):" -ForegroundColor Yellow
    $skipped | Format-Table -AutoSize | Out-String | Write-Host
}

if ($List) {
    Write-Host "-List: nothing was registered." -ForegroundColor Cyan
    exit 0
}

# --- register --------------------------------------------------------------

try {
    Invoke-RestMethod "$ApiBaseUrl/health" -TimeoutSec 5 | Out-Null
}
catch {
    throw "Registry API unreachable at $ApiBaseUrl. Start it with: uvicorn app.main:app --port 8000"
}

Write-Host "Registering $($resolved.Count) camera(s)`n" -ForegroundColor Cyan

$registered = @()

foreach ($entry in $resolved) {
    $body = @{
        global_camera_code = $entry.Code
        department_id      = $DepartmentId
        latitude           = $entry.Latitude
        longitude          = $entry.Longitude
        fov_degrees        = 70.0
        stream_url         = $entry.StreamUrl
        vms_vendor         = "LIVE-GRID"
        status             = "ACTIVE"
    }
    # Omit rather than send null: the API treats a missing azimuth as unsurveyed.
    if ($null -ne $entry.Azimuth) { $body.azimuth_angle = $entry.Azimuth }

    try {
        $camera = Invoke-RestMethod -Method Post -Uri "$ApiBaseUrl/api/v1/cameras" `
            -Headers $authHeaders -ContentType "application/json" `
            -Body ($body | ConvertTo-Json -Compress) -TimeoutSec 10
        Write-Host ("  {0,-28} {1}  ({2}, {3})" -f $entry.Code, $camera.id, $camera.latitude, $camera.longitude) -ForegroundColor Green
        $registered += [pscustomobject]@{ Code = $entry.Code; Id = $camera.id; Codec = $entry.Codec }
    }
    catch {
        $statusCode = $_.Exception.Response.StatusCode.value__
        if ($statusCode -eq 409) {
            Write-Host ("  {0,-28} already registered - skipped" -f $entry.Code) -ForegroundColor Yellow
        }
        elseif ($statusCode -in 401, 403) {
            throw "Registry rejected the API key (HTTP $statusCode). Registering a camera needs a DEPT_ADMIN key for department $DepartmentId; mint one with .\scripts\create_api_key.ps1."
        }
        else {
            Write-Host ("  {0,-28} FAILED ({1})" -f $entry.Code, $statusCode) -ForegroundColor Red
        }
    }
}

if ($registered.Count -gt 0) {
    Write-Host "`nCamera UUIDs (use with scripts\publish_test_event.py):" -ForegroundColor Cyan
    $registered | Format-Table -AutoSize | Out-String | Write-Host
}

Write-Host "Open the console at http://localhost:5173, click a GRID- marker, then Request Live Stream." -ForegroundColor Cyan
Write-Host "Restart uvicorn after editing LIVE_GRID_EMAIL / LIVE_GRID_PASSWORD - the API reads them at startup." -ForegroundColor DarkGray
