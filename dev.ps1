# dev.ps1 - Check requirements and start the docmind dev environment

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$SrcDir = Join-Path $ProjectRoot "src"
$EnvFile = Join-Path $SrcDir ".env"
$ReqFile = Join-Path $SrcDir "requirements.txt"

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  [OK]  $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  [!!]  $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "  [XX]  $msg" -ForegroundColor Red }

# --- 1. Check Python packages ---
Write-Step "Checking Python package requirements..."

# Build a hashtable of all installed packages from a single pip list call
$installed = @{}
pip list --format=columns 2>$null | Select-Object -Skip 2 | ForEach-Object {
    $parts = $_ -split '\s+'
    if ($parts.Count -ge 2) { $installed[$parts[0].ToLower()] = $parts[1] }
}

$missing = @()
foreach ($line in Get-Content $ReqFile) {
    $line = $line.Trim()
    if (-not $line -or $line.StartsWith("#")) { continue }

    $pkg = ($line -replace '\[.*?\]', '' -replace '[>=<!;].*', '').Trim()
    $version = $installed[$pkg.ToLower()]
    if ($version) {
        Write-Ok "$pkg $version"
    } else {
        Write-Fail "$pkg - not installed"
        $missing += $pkg
    }
}

if ($missing.Count -gt 0) {
    Write-Warn "$($missing.Count) package(s) missing. Installing..."
    pip install -r $ReqFile
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "pip install failed. Fix errors above and re-run."
        exit 1
    }
    Write-Ok "All packages installed."
} else {
    Write-Ok "All packages already satisfied."
}

# --- 2. Check .env ---
Write-Step "Checking .env file..."

if (-not (Test-Path $EnvFile)) {
    Write-Fail ".env not found at src/.env"
    Write-Host "  Run: Copy-Item src/.env.example src/.env  then set OPENROUTER_API_KEY" -ForegroundColor Yellow
    exit 1
}

$apiKey = (Get-Content $EnvFile | Select-String "^OPENROUTER_API_KEY=") -replace "OPENROUTER_API_KEY=", ""
if (-not $apiKey -or $apiKey -like "*your-key*" -or $apiKey -eq "") {
    Write-Fail "OPENROUTER_API_KEY is not set in src/.env"
    exit 1
}
Write-Ok ".env found and OPENROUTER_API_KEY is set."

# --- 3. Start Postgres via Docker ---
Write-Step "Starting Postgres via Docker..."

docker info | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Docker daemon is not running. Start Docker Desktop and retry."
    exit 1
}

$running = docker ps --filter "name=docmind_postgres" --filter "status=running" -q
if ($running) {
    Write-Ok "docmind_postgres is already running."
} else {
    Write-Host "  Starting container..." -ForegroundColor Gray
    docker-compose -f (Join-Path $ProjectRoot "docker-compose.yml") up postgres -d
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Failed to start Postgres container."
        exit 1
    }

    Write-Host "  Waiting for Postgres to be healthy..." -ForegroundColor Gray
    $attempts = 0
    $healthy = ""
    do {
        Start-Sleep -Seconds 2
        $healthy = docker inspect --format "{{.State.Health.Status}}" docmind_postgres
        $attempts++
    } while ($healthy -ne "healthy" -and $attempts -lt 15)

    if ($healthy -eq "healthy") {
        Write-Ok "Postgres is healthy."
    } else {
        Write-Warn "Postgres did not report healthy in time - proceeding anyway."
    }
}

# --- 4. Start FastAPI dev server ---
Write-Step "Starting FastAPI dev server..."
Write-Host "  Listening on http://localhost:8000  (Ctrl+C to stop)`n" -ForegroundColor Gray

Set-Location $SrcDir
uvicorn main:app --reload --host 0.0.0.0 --port 8000
