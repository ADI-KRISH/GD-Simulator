<#
    One GD service, in a tab of the platform's terminal window.

        pane.ps1 -Id m14-api     the API on 8114
        pane.ps1 -Id m14-web     the web app on 5214

    Opened by start_platform.ps1. Named pane.ps1 because that is how the
    platform's `start.bat stop` finds a module's service tabs - by the path of
    the script they run, never by window title.

    The model key is read HERE, into this process only: not written to disk,
    not printed, and not on a command line, where any process listing shows it.

    ASCII only and CRLF.
#>
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('m14-api', 'm14-web')]
    [string] $Id
)

$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'platform_env.ps1')
Set-GdPlatformEnv
$host.UI.RawUI.WindowTitle = "M14 $Id"

if ($Id -eq 'm14-api') {
    $key = Get-GdModelKey -Root $root
    if ($key) {
        $env:LLM_API_KEY = $key
    }
    else {
        # Runs without a model: the personas fall silent and reports cannot be
        # written, so say it where whoever is watching this tab will see it.
        Write-Host '  No model key found (GROQ_API_KEYS in keys.txt beside the repos).' -ForegroundColor Yellow
        $env:LLM_PROVIDER = 'stub'
    }
    Set-Location -LiteralPath $root
    & (Join-Path $root '.venv\Scripts\python.exe') -m uvicorn services.main:app --host 127.0.0.1 --port $GdApiPort
}
else {
    Set-Location -LiteralPath (Join-Path $root 'frontend')
    & (Join-Path $root 'frontend\node_modules\.bin\next.cmd') start -p $GdWebPort
}

Write-Host ''
Write-Host "  $Id stopped (exit $LASTEXITCODE). Close this tab, or run start_gd.bat again." -ForegroundColor Yellow
