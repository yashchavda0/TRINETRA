<#
.SYNOPSIS
    Applies every sql/*.sql migration in filename order.

.DESCRIPTION
    Runs psql inside the Postgres container, so no local PostgreSQL client
    installation is required. The sql/ directory is bind-mounted at /sql by
    docker-compose.infra.yml.

    Each migration is wrapped in its own transaction and written to be
    idempotent, so re-running this script on an existing database is safe.

.PARAMETER ComposeFile
    Compose file that defines the postgres service. Defaults to
    docker-compose.infra.yml in the repository root.

.PARAMETER Service
    Compose service name of the database. Defaults to 'postgres'.

.PARAMETER User
    PostgreSQL role to connect as. Defaults to 'trinetra'.

.PARAMETER Database
    Database to apply the migrations to. Defaults to 'trinetra'.

.EXAMPLE
    .\scripts\migrate.ps1
#>
[CmdletBinding()]
param(
    [string]$ComposeFile = "docker-compose.infra.yml",
    [string]$Service = "postgres",
    [string]$User = "trinetra",
    [string]$Database = "trinetra"
)

$ErrorActionPreference = "Stop"

# psql writes NOTICE lines to stderr (for example "extension already exists,
# skipping" on a re-run). Windows PowerShell turns any native stderr output into
# an error record, which with ErrorActionPreference=Stop aborts a migration that
# actually succeeded. Native commands are therefore invoked through this helper,
# which judges success by exit code alone - the only thing psql's -v ON_ERROR_STOP=1
# reflects failure in.
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

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$composePath = Join-Path $repoRoot $ComposeFile
if (-not (Test-Path $composePath)) {
    throw "compose file not found: $composePath"
}

$sqlDir = Join-Path $repoRoot "sql"
$migrations = Get-ChildItem -Path $sqlDir -Filter "*.sql" | Sort-Object Name
if ($migrations.Count -eq 0) {
    throw "no migrations found in $sqlDir"
}

Write-Host "Checking that the database container is up..." -ForegroundColor Cyan
$running = Invoke-Native { docker compose -f $ComposeFile ps --status running --services }
if ($LASTEXITCODE -ne 0) {
    throw "docker compose is not reachable. Start Docker Desktop, then run: docker compose -f $ComposeFile up -d"
}
if ($running -notcontains $Service) {
    throw "service '$Service' is not running. Start it with: docker compose -f $ComposeFile up -d"
}

# Wait for the healthcheck rather than racing the server's own startup.
Write-Host "Waiting for PostgreSQL to accept connections..." -ForegroundColor Cyan
$ready = $false
for ($attempt = 1; $attempt -le 30; $attempt++) {
    Invoke-Native { docker compose -f $ComposeFile exec -T $Service pg_isready -U $User -d $Database } | Out-Null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 2
}
if (-not $ready) {
    throw "PostgreSQL did not become ready. Inspect: docker compose -f $ComposeFile logs $Service"
}

foreach ($migration in $migrations) {
    Write-Host "Applying $($migration.Name)..." -ForegroundColor Cyan
    Invoke-Native {
        docker compose -f $ComposeFile exec -T $Service `
            psql -v ON_ERROR_STOP=1 -U $User -d $Database -f "/sql/$($migration.Name)"
    }
    if ($LASTEXITCODE -ne 0) {
        throw "migration failed: $($migration.Name)"
    }
}

Write-Host "`nTables now present:" -ForegroundColor Green
Invoke-Native {
    docker compose -f $ComposeFile exec -T $Service psql -U $User -d $Database -c "\dt"
}

Write-Host "`nPostGIS version:" -ForegroundColor Green
Invoke-Native {
    docker compose -f $ComposeFile exec -T $Service psql -U $User -d $Database -t -c "SELECT PostGIS_Version();"
}

Write-Host "Migrations applied." -ForegroundColor Green
