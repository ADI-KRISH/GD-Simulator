# One command to bring the whole GD Platform up on this machine.
#
#   Double-click start.bat, or run:  powershell -ExecutionPolicy Bypass -File start.ps1
#
# Safe to re-run at any time: it restarts the backend and frontend, and only
# rebuilds the frontend when its source changed since the last build.

param([switch]$NoBrowser)

$root = $PSScriptRoot
$frontend = Join-Path $root "frontend"

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "`nERROR: $text" -ForegroundColor Red; Read-Host "Press Enter to close"; exit 1 }

function Wait-Until($what, [scriptblock]$check, [int]$seconds = 120) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (& $check) { return $true }
        Start-Sleep -Seconds 2
    }
    Write-Host "   timed out waiting for $what" -ForegroundColor Yellow
    return $false
}

function Test-Url($url) {
    try { (Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200 } catch { $false }
}

function Test-PortFree($port) {
    -not (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

function Stop-Port($port) {
    $ids = (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue).OwningProcess | Select-Object -Unique
    foreach ($id in $ids) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
    # Starting the replacement before the port is released makes it exit at once.
    [void](Wait-Until "port $port to be released" { Test-PortFree $port } 20)
}

# Starts a server in its own window; if it doesn't answer at $url, retries once.
function Start-Server($title, $dir, $command, $port, $url) {
    foreach ($attempt in 1, 2) {
        Start-Window $title $dir $command
        if (Wait-Until $title { Test-Url $url } 90) { return $true }
        Write-Host "   $title did not answer - retrying once..." -ForegroundColor Yellow
        Stop-Port $port
    }
    return $false
}

function Window-Script($title, $dir, $command) {
    "Set-Location -LiteralPath '$dir'; `$host.UI.RawUI.WindowTitle = '$title'; $command"
}

function Encode($script) {
    [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
}

# Encoded, because -Command loses the quotes around paths with spaces
# ("GS Adithya Krishna", "Final year Project") and the server never starts.
function Start-Window($title, $dir, $command) {
    Start-Process powershell -ArgumentList '-NoExit', '-EncodedCommand', (Encode (Window-Script $title $dir $command))
}

# --- 0. Prerequisites -------------------------------------------------------
if (-not (Test-Path "$root\.venv\Scripts\python.exe")) { Fail "Python venv missing. See README section 2 (Backend) for first-time setup." }
if (-not (Test-Path "$root\.env")) { Copy-Item "$root\.env.example" "$root\.env"; Write-Host "Created .env from .env.example - add your LLM key to it." -ForegroundColor Yellow }

# --- 1. Docker Desktop ------------------------------------------------------
Step "Docker Desktop"
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Start-Process "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
    $ok = Wait-Until "Docker" { docker info *> $null; $LASTEXITCODE -eq 0 } 240
    if (-not $ok) { Fail "Docker Desktop did not start. Open it manually, then run this again." }
}
Write-Host "   running"

# --- 2. Infrastructure containers -------------------------------------------
Step "Infrastructure (Postgres, Redis, LiveKit, MinIO, Prometheus, Grafana)"
docker compose --env-file "$root\.env" -f "$root\infra\docker-compose.yml" up -d
if ($LASTEXITCODE -ne 0) { Fail "docker compose up failed (see above)." }
$healthy = Wait-Until "Postgres and Redis" {
    (docker inspect -f '{{.State.Health.Status}}' gd-postgres 2>$null) -eq "healthy" -and
    (docker inspect -f '{{.State.Health.Status}}' gd-redis 2>$null) -eq "healthy"
} 120
if (-not $healthy) { Fail "Postgres/Redis did not become healthy. Check: docker ps" }
Write-Host "   healthy"

# --- 3. Database migrations -------------------------------------------------
Step "Database migrations"
Push-Location $root
& "$root\.venv\Scripts\alembic.exe" upgrade head
$migrated = $LASTEXITCODE -eq 0
Pop-Location
if (-not $migrated) { Fail "Migrations failed (see above)." }

# --- 4. Stop anything left over from a previous run --------------------------
Step "Stopping old backend/frontend (if any)"
# PORTS.txt: M14 is API 8114, web 5214 - the same on its own as on the platform.
$backendCmd = ".\.venv\Scripts\python.exe -m uvicorn services.main:app --host 127.0.0.1 --port 8114"
$frontendCmd = "npm start"

# Close windows opened by a previous run, recognised by their exact encoded
# command line (MainWindowTitle is not reliable for them).
$ours = @(
    (Encode (Window-Script "GD backend" $root $backendCmd)),
    (Encode (Window-Script "GD frontend" $frontend $frontendCmd))
)
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object { $cl = $_.CommandLine; $ours | Where-Object { $cl -like "*$_*" } } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Stop-Port 8114
Stop-Port 5214

# --- 5. Frontend build (only when the source changed) ------------------------
Step "Frontend build"
if (-not (Test-Path "$frontend\node_modules")) {
    Push-Location $frontend; npm install; Pop-Location
}
$buildId = "$frontend\.next\BUILD_ID"
$sources = Get-ChildItem "$frontend\app", "$frontend\lib" -Recurse -File
$sources += Get-ChildItem "$frontend\package.json", "$frontend\next.config.mjs", "$frontend\.env.local" -ErrorAction SilentlyContinue
$newest = ($sources | Sort-Object LastWriteTime -Descending | Select-Object -First 1).LastWriteTime
if (-not (Test-Path $buildId) -or $newest -gt (Get-Item $buildId).LastWriteTime) {
    Write-Host "   source changed - building (about a minute)..."
    Push-Location $frontend; npm run build; $built = $LASTEXITCODE -eq 0; Pop-Location
    if (-not $built) { Fail "Frontend build failed (see above)." }
} else {
    Write-Host "   up to date - skipping build"
}

# --- 6. Backend and frontend, each in its own window -------------------------
Step "Starting backend and frontend"
if (-not (Start-Server "GD backend" $root $backendCmd 8114 "http://127.0.0.1:8114/health")) {
    Fail "Backend did not start. Look at the 'GD backend' window for the error."
}
if (-not (Start-Server "GD frontend" $frontend $frontendCmd 5214 "http://localhost:5214/")) {
    Fail "Frontend did not start. Look at the 'GD frontend' window for the error."
}

Write-Host "`nGD Platform is running at http://localhost:5214" -ForegroundColor Green
Write-Host "Keep the 'GD backend' and 'GD frontend' windows open while you use it."
if (-not $NoBrowser) { Start-Process "http://localhost:5214" }
