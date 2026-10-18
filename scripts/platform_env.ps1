<#
    GD as part of the AI Interviewer platform: its ports and its settings.
    Dot-sourced by start_platform.ps1 and pane.ps1, so both agree.

    Ports are PORTS.txt's: 8100 + 14 for the API, 5200 + 14 for the web app -
    the same as standalone GD's (start.bat), so run one or the other.
    No Docker: SQLite, Redis off, files on disk - the same no-Docker mode GD
    runs in standalone, with the platform bridge on (sign-in with the
    platform's session, results to the skill graph).

    ASCII only and CRLF, like every launcher script on the platform.
#>

$GdApiPort = 8114
$GdWebPort = 5214
$GdApiUrl = "http://localhost:$GdApiPort"
$GdWebUrl = "http://localhost:$GdWebPort"

function Set-GdPlatformEnv {
    $env:APP_ENV = 'local'
    $env:DATABASE_URL = 'sqlite+aiosqlite:///./gd_platform.db'
    $env:REDIS_URL = 'disabled'
    $env:OBJECT_STORE = 'filesystem'
    $env:TTS_PROVIDER = 'edge'
    # Real speech-to-text: Whisper on the same endpoint and key as the model.
    # Without it GD falls back to a stub that hears canned sentences.
    $env:STT_PROVIDER = 'openai'
    $env:OPENAI_STT_MODEL = 'whisper-large-v3-turbo'
    $env:LLM_PROVIDER = 'openai'
    $env:OPENAI_BASE_URL = 'https://api.groq.com/openai/v1'
    $env:LLM_MODEL = 'openai/gpt-oss-120b'
    $env:PERSONA_LLM_MODEL = 'openai/gpt-oss-20b'
    $env:LLM_FALLBACK_MODELS = 'openai/gpt-oss-20b'
    $env:PLATFORM_BRIDGE = '1'
    $env:M04_URL = 'http://127.0.0.1:8104'
    $env:FRONTEND_ORIGIN = $GdWebUrl
    $env:NEXT_PUBLIC_API_URL = $GdApiUrl
}

function Select-FirstKey {
    param([string] $Value)
    return ($Value -split '[,;\s]+' |
        ForEach-Object { $_.Trim().Trim('"').Trim("'") } |
        Where-Object { $_ } |
        Select-Object -First 1)
}

<#
    The model key, or $null: the environment first, then keys.txt beside the
    repos (where every module looks), then this repo's. The FIRST key only -
    one Groq organization per running app; pooling two organizations' quotas
    is what Groq's acceptable use policy forbids. Never printed.
#>
function Get-GdModelKey {
    param([string] $Root)

    foreach ($name in @('LLM_API_KEY', 'GROQ_API_KEY', 'GROQ_API_KEYS')) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if ($value) { return (Select-FirstKey $value) }
    }
    foreach ($file in @((Join-Path (Split-Path -Parent $Root) 'keys.txt'), (Join-Path $Root 'keys.txt'))) {
        if (-not (Test-Path $file)) { continue }
        $line = Get-Content -Path $file | Where-Object { $_ -match '^\s*GROQ_API_KEYS?\s*=' } | Select-Object -First 1
        if ($line) { return (Select-FirstKey (($line -split '=', 2)[1])) }
    }
    return $null
}

function Test-GdPort {
    param([int] $Port)
    return [bool] (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}
