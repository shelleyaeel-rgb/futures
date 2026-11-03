param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSCommandPath
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$appConfig = Get-Content -LiteralPath (Join-Path $projectRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$appPort = $appConfig.app.port
$appUrl = "http://127.0.0.1:$appPort"
$ready = $false
try {
    $health = Invoke-RestMethod -Uri "$appUrl/api/health" -TimeoutSec 2
    $ready = $health.mode -eq 'local_simulation'
} catch {}
if (-not $ready) {
    if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Missing .venv Python environment. See README.md.' }
    $logDirectory = Join-Path $projectRoot 'data'
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $appProcess = Start-Process -FilePath $pythonPath -ArgumentList @('-X', 'utf8', '-m', 'futures_app') -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDirectory 'app.stdout.log') -RedirectStandardError (Join-Path $logDirectory 'app.stderr.log') -PassThru
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 500
        if ($appProcess.HasExited) { throw 'Application did not start. See data/app.stderr.log.' }
        try {
            $health = Invoke-RestMethod -Uri "$appUrl/api/health" -TimeoutSec 1
            if ($health.mode -eq 'local_simulation') { $ready = $true; break }
        } catch {}
    }
}
if (-not $ready) { throw 'Application readiness timed out.' }
Write-Output "IF paper trading is running at $appUrl/"
if (-not $NoBrowser) { Start-Process "$appUrl/" }
