<#
.SYNOPSIS
    Generates the Python bindings for proto/surveillance_event.proto.

.DESCRIPTION
    The generated module is not committed: it is a build artifact of the .proto
    contract, and a stale copy on disk is worse than no copy. Re-run this after
    any change to the contract.

    Output lands in generated/surveillance_event_pb2.py, which is what
    workers/handoff_worker.py and scripts/publish_test_event.py import.

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

$protoFile = Join-Path $repoRoot "proto\surveillance_event.proto"
if (-not (Test-Path $protoFile)) {
    throw "contract not found: $protoFile"
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
python -m grpc_tools.protoc -I proto --python_out=$OutputDir proto/surveillance_event.proto
if ($LASTEXITCODE -ne 0) {
    throw "protoc failed"
}

$generated = Join-Path $outPath "surveillance_event_pb2.py"
if (-not (Test-Path $generated)) {
    throw "protoc reported success but $generated is missing"
}

# Round-trip the contract so a dimension or enum mistake surfaces here rather
# than inside the worker's consume loop.
Write-Host "Verifying the generated bindings..." -ForegroundColor Cyan
python -c @"
import sys, uuid
sys.path.insert(0, r'$repoRoot')
from generated import surveillance_event_pb2 as pb
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
print(f'ok: {len(blob)} bytes, 512-dim embedding round-tripped')
"@
if ($LASTEXITCODE -ne 0) {
    throw "generated bindings failed verification"
}

Write-Host "Bindings ready: $generated" -ForegroundColor Green
