<#
.SYNOPSIS
    Mints a service API key for the scripts that talk to the registry API.

.DESCRIPTION
    The registry authenticates every call since migration 003. People sign in
    and carry a JWT; unattended callers - the grid registration, location and
    health scripts, and the Model 4 worker - are not people and must not hold a
    human's password, so they authenticate with a key from the api_keys table
    instead.

    Only the SHA-256 hash is stored, exactly as app/auth/security.py hashes an
    incoming key, so THE PLAINTEXT IS PRINTED ONCE AND CANNOT BE RECOVERED. Lose
    it and you mint another.

    There is no API endpoint for this on purpose: an endpoint that mints
    credentials needs credentials, and this is the call that breaks that circle.
    It therefore writes to Postgres directly, through the compose container.

.PARAMETER Name
    Label stored with the key, so a key can be identified and revoked later.

.PARAMETER Role
    VIEWER, OPERATOR, DEPT_ADMIN or SUPER_ADMIN. Defaults to DEPT_ADMIN, which
    is the least privilege that covers the three grid scripts: registering and
    moving cameras needs DEPT_ADMIN, health-ping needs OPERATOR.

.PARAMETER DepartmentId
    Department the key is scoped to. Required for every role except SUPER_ADMIN.
    Defaults to POLICE, which owns the grid cameras.

.PARAMETER ExpiresInDays
    Optional expiry. Omit for a key that does not expire.

.PARAMETER Container
    Postgres container name. Defaults to trinetra-postgres.

.EXAMPLE
    .\scripts\create_api_key.ps1 -Name grid-tooling

.EXAMPLE
    .\scripts\create_api_key.ps1 -Name nightly-probe -Role OPERATOR -ExpiresInDays 90
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Name,
    [ValidateSet("VIEWER", "OPERATOR", "DEPT_ADMIN", "SUPER_ADMIN")]
    [string]$Role = "DEPT_ADMIN",
    [string]$DepartmentId = "POLICE",
    [int]$ExpiresInDays = 0,
    [string]$Container = "trinetra-postgres",
    [string]$Database = "trinetra",
    [string]$User = "trinetra"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

# A SUPER_ADMIN key is fleet-wide by definition; the table's own check
# constraint rejects a department on it.
if ($Role -eq "SUPER_ADMIN") { $DepartmentId = "" }

# --- generate ---------------------------------------------------------------

# 32 bytes of CSPRNG output, hex-encoded. Long enough that the hash cannot be
# attacked by guessing, and safe to paste into a header or an .env line.
# RandomNumberGenerator.Fill is .NET Core only; Windows PowerShell 5.1 runs on
# the desktop framework, where the provider has to be created and disposed.
$bytes = New-Object byte[] 32
$rng = [System.Security.Cryptography.RNGCryptoServiceProvider]::new()
try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
$plaintext = "trn_" + (($bytes | ForEach-Object { $_.ToString("x2") }) -join '')

# Must match app/auth/security.py:hash_api_key - plain SHA-256 of the UTF-8
# bytes, lowercase hex. A mismatch here produces a key that authenticates
# nowhere, with a 401 that says nothing about why.
$sha = [System.Security.Cryptography.SHA256]::Create()
try {
    $hashBytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($plaintext))
}
finally {
    $sha.Dispose()
}
$keyHash = (($hashBytes | ForEach-Object { $_.ToString("x2") }) -join '')

# --- insert -----------------------------------------------------------------

$departmentSql = if ([string]::IsNullOrWhiteSpace($DepartmentId)) { "NULL" } else { "'" + $DepartmentId.ToUpperInvariant().Replace("'", "''") + "'" }
$expiresSql = if ($ExpiresInDays -gt 0) { "clock_timestamp() + interval '$ExpiresInDays days'" } else { "NULL" }
$nameSql = "'" + $Name.Replace("'", "''") + "'"

$sql = @"
INSERT INTO api_keys (name, key_hash, role, department_id, expires_at)
VALUES ($nameSql, '$keyHash', '$Role', $departmentSql, $expiresSql)
RETURNING id;
"@

try {
    $result = $sql | docker exec -i $Container psql -U $User -d $Database -t -A -v ON_ERROR_STOP=1 -f - 2>&1
}
catch {
    throw "Could not reach Postgres in container '$Container'. Start it with: docker compose -f docker-compose.infra.yml up -d postgres"
}
if ($LASTEXITCODE -ne 0) {
    throw "Key insert failed: $result"
}

$keyId = ($result | Where-Object { $_ -match '^[0-9a-f-]{36}$' } | Select-Object -First 1)

# --- report -----------------------------------------------------------------

Write-Host "`nAPI key created" -ForegroundColor Green
Write-Host ("  name       {0}" -f $Name)
Write-Host ("  id         {0}" -f $keyId)
Write-Host ("  role       {0}{1}" -f $Role, $(if ($DepartmentId) { " (department $($DepartmentId.ToUpperInvariant()))" } else { " (fleet-wide)" }))
Write-Host ("  expires    {0}" -f $(if ($ExpiresInDays -gt 0) { "in $ExpiresInDays day(s)" } else { "never" }))

Write-Host "`nThe key is shown once - only its hash is stored:`n" -ForegroundColor Yellow
Write-Host "  $plaintext`n" -ForegroundColor Cyan

Write-Host "Use it by adding this line to .env:" -ForegroundColor DarkGray
Write-Host "  TRINETRA_API_KEY=$plaintext" -ForegroundColor DarkGray
Write-Host "The grid scripts read it from there, and send it as the X-API-Key header." -ForegroundColor DarkGray
Write-Host "Revoke it with:  UPDATE api_keys SET is_active = false WHERE id = '$keyId';" -ForegroundColor DarkGray
