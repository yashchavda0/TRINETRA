<#
.SYNOPSIS
    Generates the Python bindings for the analytics bus proto contracts.

.DESCRIPTION
    Generated modules are not committed: they are build artifacts of the .proto
    contracts, and a stale copy on disk is worse than no copy. Re-run this after
    any change to either contract.

    Output lands in generated/surveillance_event_pb2.py and
    generated/scene_event_pb2.py, which workers/handoff_worker.py,
    workers/scene_event_worker.py, services/anpr, services/vlm_agent and
    scripts/publish_test_event.py import.

.EXAMPLE
    .\scripts\gen_proto.ps1
#>
[CmdletBinding()]
param(
    [string]$OutputDir = "generated"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$protoFiles = @("proto\surveillance_event.proto", "proto\scene_event.proto")
foreach ($proto in $protoFiles) {
    $fullPath = Join-Path $repoRoot $proto
    if (-not (Test-Path $fullPath)) {
        throw "contract not found: $fullPath"
    }
}

python -c "import grpc_tools" *> $null
if ($LASTEXITCODE -ne 0) {
    throw "grpcio-tools is not installed. Run: pip install -r requirements.txt"
}

$outPath = Join-Path $repoRoot $OutputDir
if (-not (Test-Path $outPath)) {
    New-Item -ItemType Directory -Path $outPath | Out-Null
}

Write-Host "Generating Python bindings into $OutputDir ..." -ForegroundColor Cyan
# One protoc invocation for both files. They do not import each other -
# see the header comment in scene_event.proto for why - so this is purely for
# convenience, not because either resolves a symbol in the other.
python -m grpc_tools.protoc -I proto --python_out=$OutputDir $protoFiles
if ($LASTEXITCODE -ne 0) {
    throw "protoc failed"
}

$surveillanceOut = Join-Path $outPath "surveillance_event_pb2.py"
$sceneOut = Join-Path $outPath "scene_event_pb2.py"
foreach ($generated in @($surveillanceOut, $sceneOut)) {
    if (-not (Test-Path $generated)) {
        throw "protoc reported success but $generated is missing"
    }
}

# Round-trip both contracts so a dimension or enum mistake surfaces here rather
# than inside a worker's consume loop.
Write-Host "Verifying the generated bindings..." -ForegroundColor Cyan
python -c @"
import sys, uuid
sys.path.insert(0, r'$repoRoot')
from generated import surveillance_event_pb2 as pb
from generated import scene_event_pb2 as scene_pb

event = pb.SurveillanceEvent(
    event_id=str(uuid.uuid4()),
    camera_id=str(uuid.uuid4()),
    department_code=pb.POLICE,
    timestamp_utc_ms=1757000000123,
    location=pb.GeoLocation(latitude=23.0225, longitude=72.5714, azimuth_degrees=137.5),
    object_class=pb.MOTORCYCLE,
    feature_embedding=[0.0] * 512,
)
blob = event.SerializeToString()
decoded = pb.SurveillanceEvent()
decoded.ParseFromString(blob)
assert len(decoded.feature_embedding) == 512
print(f'ok: surveillance_event, {len(blob)} bytes, 512-dim embedding round-tripped')

scene = scene_pb.SceneEvent(
    scene_event_id=str(uuid.uuid4()),
    camera_id=str(uuid.uuid4()),
    window_start_utc_ms=1757000000123,
    window_end_utc_ms=1757000005123,
    latitude=23.0225,
    longitude=72.5714,
    event_type=scene_pb.LOITERING,
    confidence=0.82,
    rationale='one subject stationary near the gate for 4 minutes',
    implicated_target_ids=['abc123'],
    model_version='test@dev',
)
scene_blob = scene.SerializeToString()
decoded_scene = scene_pb.SceneEvent()
decoded_scene.ParseFromString(scene_blob)
assert decoded_scene.event_type == scene_pb.LOITERING
print(f'ok: scene_event, {len(scene_blob)} bytes round-tripped')
"@
if ($LASTEXITCODE -ne 0) {
    throw "generated bindings failed verification"
}

Write-Host "Bindings ready: $surveillanceOut, $sceneOut" -ForegroundColor Green
