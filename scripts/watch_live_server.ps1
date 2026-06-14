param(
    [int]$Port = 8000,
    [int]$IntervalSeconds = 60
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$WatchdogLog = Join-Path $LogDir "watchdog.log"
$HealthUrl = "http://127.0.0.1:$Port/api/health"
$StartScript = Join-Path $PSScriptRoot "start_live_server.ps1"
$CreatedNew = $false
$Mutex = New-Object System.Threading.Mutex($true, "Global\StockRlTraderWatchdog-$Port", [ref]$CreatedNew)

function Write-WatchdogLog($Message) {
    $Time = Get-Date -Format "yyyy-MM-dd HH:mm:ss KST"
    Add-Content -Path $WatchdogLog -Value "$Time $Message"
}

if (-not $CreatedNew) {
    Write-WatchdogLog "watchdog already running for port $Port"
    exit 0
}

Write-WatchdogLog "watchdog started for $HealthUrl interval=${IntervalSeconds}s"

while ($true) {
    try {
        Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 5 | Out-Null
    } catch {
        Write-WatchdogLog "health check failed: $($_.Exception.Message)"
        try {
            & $StartScript -Port $Port
        } catch {
            Write-WatchdogLog "restart failed: $($_.Exception.Message)"
        }
    }
    Start-Sleep -Seconds $IntervalSeconds
}
