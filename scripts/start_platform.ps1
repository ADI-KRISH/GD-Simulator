<#
    GD as part of the AI Interviewer platform (start_gd.bat).

    Brings the API up on 8114 and the web app on 5214, each in a tab of the
    platform's terminal window ('ai-interviewer'), so the platform's
    start.bat finds it beside the other modules and its `stop` closes it.
    Returns once both are listening; a service already listening is left alone.

    Safe to re-run. The web app is rebuilt only when its source changed since
    the last platform build. Standalone GD (start.bat, Docker, 8000/3000) is
    untouched: different ports, database and build folder.

    ASCII only and CRLF.
#>
param(
    [string] $Window = 'ai-interviewer'
)

$root = Split-Path -Parent $PSScriptRoot
$frontend = Join-Path $root 'frontend'
. (Join-Path $PSScriptRoot 'platform_env.ps1')

function Fail([string] $Text) {
    Write-Host "  [M14] $Text" -ForegroundColor Red
    exit 1
}

if ((Test-GdPort $GdApiPort) -and (Test-GdPort $GdWebPort)) {
    Write-Host "  [M14] already listening on $GdApiPort and $GdWebPort" -ForegroundColor DarkGray
    exit 0
}

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { Fail 'no .venv - see README section 2 (Backend) for first-time setup.' }
if (-not (Test-Path (Join-Path $frontend 'node_modules'))) {
    Push-Location $frontend; npm install; Pop-Location
}

Set-GdPlatformEnv

# -- the database: migrations are idempotent ----------------------------------
Push-Location $root
& (Join-Path $root '.venv\Scripts\alembic.exe') upgrade head *> $null
$migrated = $LASTEXITCODE -eq 0
Pop-Location
if (-not $migrated) { Fail 'database migrations failed: run  .venv\Scripts\alembic.exe upgrade head  to see why.' }

# -- the web app: built for 8114, only when its source changed -----------------
$dist = Join-Path $frontend $GdDistDir
$stamp = Join-Path $dist 'api_url.txt'
$sources = @(Get-ChildItem (Join-Path $frontend 'app'), (Join-Path $frontend 'lib') -Recurse -File)
$sources += @(Get-ChildItem (Join-Path $frontend 'package.json'), (Join-Path $frontend 'next.config.mjs') -ErrorAction SilentlyContinue)
$newest = ($sources | Sort-Object LastWriteTime -Descending | Select-Object -First 1).LastWriteTime
$buildId = Join-Path $dist 'BUILD_ID'
$stale = -not (Test-Path $buildId) -or
    $newest -gt (Get-Item $buildId).LastWriteTime -or
    -not (Test-Path $stamp) -or
    ((Get-Content -Raw $stamp).Trim() -ne $GdApiUrl)
if ($stale -and -not (Test-GdPort $GdWebPort)) {
    Write-Host '  [M14] building the web app for the platform (about a minute)...' -ForegroundColor DarkGray
    Push-Location $frontend
    & (Join-Path $frontend 'node_modules\.bin\next.cmd') build *> (Join-Path $root 'gd_platform_build.log')
    $built = $LASTEXITCODE -eq 0
    Pop-Location
    if (-not $built) { Fail 'the web app did not build: see gd_platform_build.log.' }
    Set-Content -Path $stamp -Value $GdApiUrl -Encoding ascii
}

# -- the two tabs ---------------------------------------------------------------
$haveWt = [bool] (Get-Command 'wt.exe' -ErrorAction SilentlyContinue)
$pane = Join-Path $PSScriptRoot 'pane.ps1'
$services = @(
    @{ Id = 'm14-api'; Label = 'M14 API'; Port = $GdApiPort },
    @{ Id = 'm14-web'; Label = 'M14 Web'; Port = $GdWebPort }
)
foreach ($s in $services) {
    if (Test-GdPort $s.Port) { continue }
    $psArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $pane, '-Id', $s.Id)
    if ($haveWt) {
        & wt.exe -w $Window new-tab --title $s.Label -d $root powershell @psArgs
    }
    else {
        $null = Start-Process -FilePath 'powershell' -ArgumentList $psArgs -WorkingDirectory $root
    }
    # One at a time: the named window must exist before the next `wt -w`, or
    # Terminal opens a second window.
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline -and -not (Test-GdPort $s.Port)) { Start-Sleep -Milliseconds 400 }
    if (-not (Test-GdPort $s.Port)) { Fail "$($s.Label) did not start on $($s.Port): look at its tab." }
}

Write-Host "  [M14] group discussion: API $GdApiUrl, web $GdWebUrl (open it from the platform at /gd)" -ForegroundColor Green
