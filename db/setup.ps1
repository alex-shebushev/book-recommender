<#
Book Recommender - local database setup (Windows PowerShell).

One command: starts Postgres in Docker, waits for it, creates a Python venv,
installs the migration runner's deps, and applies all pending migrations.
Safe to re-run: it only creates what's missing and applies new migrations.

Prerequisites:
  * Docker Desktop is running.
  * Python 3.10+ is on PATH.

Run (from anywhere):
  powershell -ExecutionPolicy Bypass -File "C:\Projects\AI Course\Book Recommender\db\setup.ps1"
#>
$ErrorActionPreference = 'Stop'

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path   # ...\db
$ProjectDir = Split-Path -Parent $ScriptDir                     # ...\Book Recommender
Set-Location $ProjectDir

# 1. Ensure .env exists.
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example"
}

# Read user/db/port from .env (fall back to defaults).
function Get-EnvValue($name, $default) {
    $line = Get-Content ".env" | Where-Object { $_ -match "^$name=" } | Select-Object -First 1
    if ($line) { return ($line -replace "^$name=", "").Trim() }
    return $default
}
$pgUser = Get-EnvValue "POSTGRES_USER" "bookrec"
$pgDb   = Get-EnvValue "POSTGRES_DB"   "bookrec"
$pgPort = Get-EnvValue "POSTGRES_PORT" "5544"

# 2. Start Postgres.
Write-Host "Starting Postgres container..."
docker compose up -d
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed. Is Docker Desktop running?" }

# 3. Wait until it accepts connections.
Write-Host "Waiting for the database..."
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    docker compose exec -T db pg_isready -U $pgUser -d $pgDb *> $null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 1
}
if (-not $ready) { throw "Database did not become ready in time." }
Write-Host "Database is ready."

# 4. Python venv + migration deps.
$venv = Join-Path $ScriptDir ".venv"
$py   = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Creating virtual environment (db/.venv)..."
    python -m venv $venv
}
Write-Host "Installing migration dependencies..."
& $py -m pip install -q --upgrade pip
& $py -m pip install -q -r (Join-Path $ScriptDir "requirements.txt")

# 5. Apply migrations.
Write-Host "Applying migrations..."
& $py (Join-Path $ScriptDir "migrate.py")
if ($LASTEXITCODE -ne 0) { throw "Migration failed." }

# 6. Summary.
Write-Host ""
Write-Host "Done. Connect with:"
Write-Host "  psql postgresql://${pgUser}:<password>@localhost:${pgPort}/${pgDb}"
Write-Host "Migration status:"
& $py (Join-Path $ScriptDir "migrate.py") --status
