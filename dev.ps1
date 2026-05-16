# dev.ps1 - Check requirements and start the docmind dev environment

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$SrcDir      = Join-Path $ProjectRoot "src"
$EnvFile     = Join-Path $SrcDir ".env"
$ReqFile     = Join-Path $ProjectRoot "requirements.txt"

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  [OK]  $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  [!!]  $msg" -ForegroundColor Yellow }

# Read a key's value from a dotenv file (strips surrounding quotes)
function Get-EnvValue([string]$file, [string]$key) {
    $line = Get-Content $file | Where-Object { $_ -match "^${key}=" } | Select-Object -First 1
    if (-not $line) { return "" }
    $val = ($line -split "=", 2)[1]
    return $val.Trim().Trim('"').Trim("'")
}

# ---------------------------------------------------------------------------
# 1. Check Python packages
# ---------------------------------------------------------------------------
Write-Step "Checking Python package requirements..."

$installed = @{}
pip list --format=columns | Select-Object -Skip 2 | ForEach-Object {
    $parts = $_ -split "\s+"
    if ($parts.Count -ge 2) { $installed[$parts[0].ToLower()] = $parts[1] }
}

$missing = @()
foreach ($line in Get-Content $ReqFile) {
    $line = $line.Trim()
    if (-not $line -or $line.StartsWith("#")) { continue }

    # Strip extras ([standard]), version specifiers (>=, ==, etc.) and env markers (;)
    $pkg = ($line -replace "\[.*?\]", "" -replace "[>=<!;].*", "").Trim()
    if (-not $pkg) { continue }

    $version = $installed[$pkg.ToLower()]
    if ($version) {
        Write-Ok "$pkg $version"
    } else {
        Write-Host "  [--]  $pkg - not installed" -ForegroundColor Yellow
        $missing += $pkg
    }
}

if ($missing.Count -gt 0) {
    Write-Warn "$($missing.Count) package(s) missing. Running pip install..."
    pip install -r $ReqFile
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [XX]  pip install failed. Fix errors above and re-run." -ForegroundColor Red
        exit 1
    }
    Write-Ok "All packages installed."
} else {
    Write-Ok "All packages already satisfied."
}

# ---------------------------------------------------------------------------
# 2. Check .env
# ---------------------------------------------------------------------------
Write-Step "Checking .env file..."

if (-not (Test-Path $EnvFile)) {
    Write-Host "  [XX]  .env not found at src/.env" -ForegroundColor Red
    Write-Host "  Create src/.env and set at least:" -ForegroundColor Yellow
    Write-Host "    OPENROUTER_API_KEY=sk-or-..." -ForegroundColor Yellow
    Write-Host "    VOYAGE_API_KEY=pa-..." -ForegroundColor Yellow
    Write-Host "    DATABASE_URL=postgresql+asyncpg://docmind:secret@localhost:5433/docmind" -ForegroundColor Yellow
    exit 1
}

$openrouterKey = Get-EnvValue $EnvFile "OPENROUTER_API_KEY"
if (-not $openrouterKey -or $openrouterKey -like "*your*") {
    Write-Host "  [XX]  OPENROUTER_API_KEY is missing or not set in src/.env" -ForegroundColor Red
    exit 1
}
Write-Ok "OPENROUTER_API_KEY is set."

$voyageKey = Get-EnvValue $EnvFile "VOYAGE_API_KEY"
if (-not $voyageKey -or $voyageKey -like "*your*") {
    Write-Host "  [XX]  VOYAGE_API_KEY is missing or not set in src/.env" -ForegroundColor Red
    exit 1
}
Write-Ok "VOYAGE_API_KEY is set."

# ---------------------------------------------------------------------------
# 3. Start Postgres via Docker
# ---------------------------------------------------------------------------
Write-Step "Starting Postgres via Docker..."

# Detect docker compose (v2 preferred over legacy v1)
$useDockerCompose = $false
$useLegacyCompose = $false

docker compose version | Out-Null
if ($LASTEXITCODE -eq 0) {
    $useDockerCompose = $true
} else {
    docker-compose version | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $useLegacyCompose = $true
    }
}

if (-not $useDockerCompose -and -not $useLegacyCompose) {
    Write-Host "  [XX]  Neither 'docker compose' nor 'docker-compose' found." -ForegroundColor Red
    Write-Host "        Install Docker Desktop and retry." -ForegroundColor Yellow
    exit 1
}

docker info | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [XX]  Docker daemon is not running. Start Docker Desktop and retry." -ForegroundColor Red
    exit 1
}

$running = docker ps --filter "name=docmind_postgres" --filter "status=running" -q
if ($running) {
    Write-Ok "docmind_postgres is already running."
} else {
    Write-Host "  Starting postgres container..." -ForegroundColor Gray

    $composeFile = Join-Path $ProjectRoot "docker-compose.yml"
    if ($useDockerCompose) {
        docker compose -f $composeFile up postgres -d
    } else {
        docker-compose -f $composeFile up postgres -d
    }

    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [XX]  Failed to start Postgres container." -ForegroundColor Red
        exit 1
    }

    Write-Host "  Waiting for Postgres to be healthy..." -ForegroundColor Gray
    $attempts = 0
    $healthy  = ""
    do {
        Start-Sleep -Seconds 2
        $healthy = (docker inspect --format "{{.State.Health.Status}}" docmind_postgres)
        $attempts++
    } while ($healthy -ne "healthy" -and $attempts -lt 15)

    if ($healthy -eq "healthy") {
        Write-Ok "Postgres is healthy."
    } else {
        Write-Warn "Postgres did not report healthy after 30s - proceeding anyway."
    }
}

# ---------------------------------------------------------------------------
# 4. Start FastAPI dev server
# ---------------------------------------------------------------------------
Write-Step "Starting FastAPI dev server..."
Write-Host "  Listening on http://localhost:8000  (Ctrl+C to stop)" -ForegroundColor Gray
Write-Host ""

Set-Location $SrcDir
uvicorn main:app --reload --host 0.0.0.0 --port 8000
