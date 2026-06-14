param(
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$WatchdogLog = Join-Path $LogDir "watchdog.log"
function Write-WatchdogLog($Message) {
    $Time = Get-Date -Format "yyyy-MM-dd HH:mm:ss KST"
    Add-Content -Path $WatchdogLog -Value "$Time $Message"
}

$Existing = netstat -ano | Select-String ":$Port\s+.*LISTENING" | Select-Object -First 1
if ($Existing) {
    Write-WatchdogLog "server already listening on port $Port"
    exit 0
}

$Python = (Get-Command python).Source
Write-WatchdogLog "starting server on port $Port"
Start-Process -FilePath $Python `
    -ArgumentList "run.py" `
    -WorkingDirectory $Root `
    -WindowStyle Hidden
