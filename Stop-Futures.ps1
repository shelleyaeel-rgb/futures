$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSCommandPath
$appConfig = Get-Content -LiteralPath (Join-Path $projectRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$appUrl = "http://127.0.0.1:$($appConfig.app.port)"
try {
    $bootstrap = Invoke-RestMethod -Uri "$appUrl/api/bootstrap" -TimeoutSec 3
} catch {
    Write-Output 'IF paper trading is not running.'
    exit 0
}
$headers = @{Origin = $appUrl; 'X-CSRF-Token' = $bootstrap.csrf_token}
$result = Invoke-RestMethod -Uri "$appUrl/api/control" -Method Post -ContentType 'application/json' -Headers $headers -Body '{"action":"shutdown"}' -TimeoutSec 10
Write-Output 'Saving local state and stopping IF paper trading.'
