<#
.SYNOPSIS
    Mints a local development CA and the mTLS material both Go services require.

.DESCRIPTION
    cmd/ingestion_gateway and cmd/stream_relay both set
    tls.RequireAndVerifyClientCert and exit at startup without cert, key and CA
    files. This script produces them with openssl running inside a container, so
    nothing has to be installed on Windows.

    Output (all under certs/, which is gitignored):
        ca.crt / ca.key                 development certificate authority
        ingest-server.crt / .key        cmd/ingestion_gateway server identity
        relay-server.crt / .key         cmd/stream_relay server identity
        api-client.crt / .key           FastAPI's identity when calling the relay
        test-client.crt / .key          for curl-ing the gateway by hand

    DEVELOPMENT ONLY. These keys are unencrypted, valid for localhost, and the
    CA private key sits next to the certificates it signs. Production material
    must come from the department PKI, not from this script.

.EXAMPLE
    .\scripts\gen_dev_certs.ps1
#>
[CmdletBinding()]
param(
    [int]$ValidityDays = 365,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$certDir = Join-Path $repoRoot "certs"
if (-not (Test-Path $certDir)) {
    New-Item -ItemType Directory -Path $certDir | Out-Null
}

if ((Test-Path (Join-Path $certDir "ca.crt")) -and -not $Force) {
    Write-Host "certs/ca.crt already exists. Re-run with -Force to regenerate everything." -ForegroundColor Yellow
    Write-Host "Regenerating invalidates every certificate already issued by the old CA." -ForegroundColor Yellow
    exit 0
}

docker version *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker is not reachable. Start Docker Desktop and try again."
}

# Kept as a bash script so the whole chain runs in one container invocation.
$script = @'
set -eu
apk add --no-cache openssl >/dev/null

cd /certs
DAYS="$1"

cat > openssl-server.cnf <<'CNF'
[req]
distinguished_name = dn
req_extensions     = ext
prompt             = no
[dn]
CN = localhost
O  = TRINETRA Development
[ext]
basicConstraints = critical, CA:FALSE
keyUsage         = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName   = DNS:localhost, DNS:host.docker.internal, IP:127.0.0.1, IP:::1
CNF

cat > openssl-client.cnf <<'CNF'
[req]
distinguished_name = dn
req_extensions     = ext
prompt             = no
[dn]
CN = trinetra-api
O  = TRINETRA Development
[ext]
basicConstraints = critical, CA:FALSE
keyUsage         = critical, digitalSignature
extendedKeyUsage = clientAuth
subjectAltName   = DNS:trinetra-api
CNF

echo "==> certificate authority"
openssl req -x509 -newkey rsa:4096 -sha256 -nodes \
  -keyout ca.key -out ca.crt -days "$DAYS" \
  -subj "/CN=TRINETRA Development CA/O=TRINETRA Development" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null

issue_server() {
  name="$1"
  echo "==> server certificate: $name"
  openssl req -newkey rsa:2048 -nodes -keyout "$name.key" -out "$name.csr" \
    -config openssl-server.cnf 2>/dev/null
  openssl x509 -req -in "$name.csr" -CA ca.crt -CAkey ca.key -CAcreateserial \
    -out "$name.crt" -days "$DAYS" -sha256 \
    -extfile openssl-server.cnf -extensions ext 2>/dev/null
  rm -f "$name.csr"
}

issue_client() {
  name="$1"
  cn="$2"
  echo "==> client certificate: $name"
  openssl req -newkey rsa:2048 -nodes -keyout "$name.key" -out "$name.csr" \
    -config openssl-client.cnf -subj "/CN=$cn/O=TRINETRA Development" 2>/dev/null
  openssl x509 -req -in "$name.csr" -CA ca.crt -CAkey ca.key -CAcreateserial \
    -out "$name.crt" -days "$DAYS" -sha256 \
    -extfile openssl-client.cnf -extensions ext 2>/dev/null
  rm -f "$name.csr"
}

issue_server ingest-server
issue_server relay-server
issue_client api-client trinetra-api
issue_client test-client trinetra-test-client

rm -f openssl-server.cnf openssl-client.cnf ca.srl
chmod 644 *.crt
chmod 600 *.key

echo "==> issued:"
ls -1 *.crt *.key
'@

# Normalise to LF: a CRLF heredoc breaks sh inside the container.
$scriptLf = $script -replace "`r`n", "`n"
$scriptPath = Join-Path $certDir "_gen.sh"
[System.IO.File]::WriteAllText($scriptPath, $scriptLf, (New-Object System.Text.UTF8Encoding($false)))

try {
    Write-Host "Generating development PKI in certs/ ..." -ForegroundColor Cyan
    docker run --rm -v "${certDir}:/certs" alpine:3.20 sh /certs/_gen.sh $ValidityDays
    if ($LASTEXITCODE -ne 0) {
        throw "certificate generation failed"
    }
}
finally {
    Remove-Item $scriptPath -Force -ErrorAction SilentlyContinue
}

Write-Host "`nDevelopment PKI written to certs/. Matching .env entries:" -ForegroundColor Green
Write-Host @"
INGEST_TLS_CERT_FILE=certs/ingest-server.crt
INGEST_TLS_KEY_FILE=certs/ingest-server.key
INGEST_TLS_CA_FILE=certs/ca.crt
STREAM_RELAY_TLS_CERT_FILE=certs/relay-server.crt
STREAM_RELAY_TLS_KEY_FILE=certs/relay-server.key
STREAM_RELAY_TLS_CA_FILE=certs/ca.crt
RELAY_CLIENT_CERT_FILE=certs/api-client.crt
RELAY_CLIENT_KEY_FILE=certs/api-client.key
RELAY_CA_FILE=certs/ca.crt
"@
Write-Host "Development use only - do not deploy these keys." -ForegroundColor Yellow
