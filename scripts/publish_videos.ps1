<#
.SYNOPSIS
    Publishes every video file in videos\ to MediaMTX as a looping RTSP stream.

.DESCRIPTION
    Each file becomes one continuously looping RTSP path, so it behaves like a
    live camera: never ends, plays at real time, and can be reconnected to.

    ffmpeg runs inside a container attached to the compose network, so ffmpeg
    does NOT need to be installed on Windows. One container per video, named
    trinetra-pub-<slug>.

    Video is transcoded to H.264 baseline and audio to Opus. That is not
    gratuitous: browsers decode baseline without renegotiation, and the console
    offers an audio m-line (GISMap.jsx:500) that a silent stream would leave
    unfulfilled.

.PARAMETER VideoDir
    Folder to scan. Defaults to videos\ in the repository root.

.PARAMETER List
    Show running publishers and their RTSP paths, then exit.

.PARAMETER Stop
    Stop and remove all publisher containers, then exit.

.EXAMPLE
    .\scripts\publish_videos.ps1
    .\scripts\publish_videos.ps1 -List
    .\scripts\publish_videos.ps1 -Stop
#>
[CmdletBinding()]
param(
    [string]$VideoDir = "videos",
    [string]$ComposeFile = "docker-compose.infra.yml",
    [string]$FfmpegImage = "jrottenberg/ffmpeg:6-ubuntu",
    [switch]$List,
    [switch]$Stop
)

$ErrorActionPreference = "Stop"

# Docker writes progress to stderr, which Windows PowerShell turns into a
# terminating error. Judge these calls by exit code instead.
function Invoke-Native {
    param([Parameter(Mandatory = $true)][scriptblock]$Command)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Command 2>&1 | ForEach-Object { "$_" }
    }
    finally {
        $ErrorActionPreference = $previous
    }
}

function ConvertTo-Slug {
    param([Parameter(Mandatory = $true)][string]$Name)

    $slug = $Name.ToLowerInvariant() -replace '[^a-z0-9]+', '-'
    return $slug.Trim('-')
}

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$prefix = "trinetra-pub-"

if ($List) {
    Write-Host "Running publishers:" -ForegroundColor Cyan
    $names = Invoke-Native { docker ps --filter "name=$prefix" --format "{{.Names}}`t{{.Status}}" }
    if (-not $names) {
        Write-Host "  (none)" -ForegroundColor Yellow
    }
    else {
        foreach ($line in $names) {
            $slug = ($line -split "`t")[0] -replace "^$prefix", ""
            Write-Host "  $line   ->  rtsp://127.0.0.1:8554/$slug"
        }
    }

    Write-Host "`nPaths known to MediaMTX:" -ForegroundColor Cyan
    try {
        $paths = Invoke-RestMethod "http://127.0.0.1:9997/v3/paths/list" -TimeoutSec 5
        foreach ($p in $paths.items) {
            $state = if ($p.ready) { "ready" } else { "NOT ready" }
            Write-Host ("  {0,-30} {1,-10} {2} bytes received" -f $p.name, $state, $p.bytesReceived)
        }
    }
    catch {
        Write-Host "  control API unreachable - is the mediamtx container up?" -ForegroundColor Yellow
    }
    exit 0
}

if ($Stop) {
    $names = Invoke-Native { docker ps -a --filter "name=$prefix" --format "{{.Names}}" }
    if (-not $names) {
        Write-Host "No publisher containers to stop." -ForegroundColor Yellow
        exit 0
    }
    foreach ($name in $names) {
        Write-Host "Removing $name ..." -ForegroundColor Cyan
        Invoke-Native { docker rm -f $name } | Out-Null
    }
    Write-Host "All publishers stopped." -ForegroundColor Green
    exit 0
}

# --- publish ---------------------------------------------------------------

# Accept either a path relative to the repository root or an absolute one.
# Join-Path would otherwise produce "C:\repo\C:\elsewhere" for an absolute value.
$videoPath = if ([System.IO.Path]::IsPathRooted($VideoDir)) {
    $VideoDir
}
else {
    Join-Path $repoRoot $VideoDir
}

if (-not (Test-Path $videoPath)) {
    New-Item -ItemType Directory -Path $videoPath | Out-Null
    Write-Host "Created $VideoDir\. Copy your test videos in and re-run this script." -ForegroundColor Yellow
    exit 0
}

$videos = Get-ChildItem -Path $videoPath -File |
    Where-Object { $_.Extension -in @(".mp4", ".mkv", ".mov", ".ts", ".avi", ".webm") }

if ($videos.Count -eq 0) {
    Write-Host "No video files found in $VideoDir\ (.mp4 .mkv .mov .ts .avi .webm)." -ForegroundColor Yellow
    exit 0
}

# Publishers reach MediaMTX by service name over the compose network, so the
# stack must be up first.
$network = Invoke-Native {
    docker inspect trinetra-mediamtx --format "{{range `$k, `$v := .NetworkSettings.Networks}}{{`$k}}{{end}}"
}
if ($LASTEXITCODE -ne 0 -or -not $network) {
    throw "trinetra-mediamtx is not running. Start it with: docker compose -f $ComposeFile up -d"
}
$network = ($network | Select-Object -First 1).Trim()

Write-Host "Publishing $($videos.Count) video(s) to MediaMTX on network $network`n" -ForegroundColor Cyan

foreach ($video in $videos) {
    $slug = ConvertTo-Slug -Name $video.BaseName
    if (-not $slug) {
        Write-Host "  skipping $($video.Name): filename has no usable characters" -ForegroundColor Yellow
        continue
    }

    $container = "$prefix$slug"
    Invoke-Native { docker rm -f $container } | Out-Null

    # NOT $args: that is a PowerShell automatic variable holding the enclosing
    # scope's own arguments, so splatting it would run bare `docker` - which
    # prints usage and exits 0, reporting success while starting nothing.
    $dockerArgs = @(
        "run", "-d",
        "--name", $container,
        "--network", $network,
        "--restart", "unless-stopped",
        "-v", "$($videoPath):/videos:ro",
        $FfmpegImage,
        "-hide_banner", "-loglevel", "warning",
        # -stream_loop -1 replays forever; -re paces output at real time so the
        # stream behaves like a camera instead of flooding in as fast as it decodes.
        "-stream_loop", "-1", "-re",
        "-i", "/videos/$($video.Name)",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-profile:v", "baseline", "-pix_fmt", "yuv420p",
        # ~2s keyframe interval: a viewer joining mid-stream sees a picture quickly.
        "-g", "50",
        "-c:a", "libopus", "-ar", "48000", "-ac", "2",
        "-f", "rtsp", "-rtsp_transport", "tcp",
        "rtsp://mediamtx:8554/$slug"
    )

    $output = Invoke-Native { docker @dockerArgs }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  FAILED  $($video.Name)" -ForegroundColor Red
        Write-Host "          $output" -ForegroundColor Red
        continue
    }

    # Confirm it is actually running: ffmpeg can exit immediately on an
    # unsupported input, and `docker run -d` succeeds regardless.
    Start-Sleep -Milliseconds 800
    $state = Invoke-Native { docker inspect $container --format "{{.State.Status}}" }
    if ("$state".Trim() -ne "running") {
        Write-Host "  FAILED  $($video.Name) - publisher exited ($state)" -ForegroundColor Red
        Invoke-Native { docker logs --tail 5 $container } | ForEach-Object {
            Write-Host "          $_" -ForegroundColor Red
        }
        continue
    }

    Write-Host ("  {0,-40} -> rtsp://127.0.0.1:8554/{1}" -f $video.Name, $slug) -ForegroundColor Green
}

Write-Host "`nCheck ingest:  .\scripts\publish_videos.ps1 -List" -ForegroundColor Cyan
Write-Host "Watch one:     http://127.0.0.1:8889/<slug>" -ForegroundColor Cyan
Write-Host "Register them: .\scripts\register_video_cameras.ps1" -ForegroundColor Cyan
Write-Host "Logs:          docker logs $prefix<slug>" -ForegroundColor Cyan
