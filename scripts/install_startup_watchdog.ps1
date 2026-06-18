param(
    [int]$Port = 8000,
    [int]$IntervalSeconds = 60
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$WatchScript = Join-Path $PSScriptRoot "watch_live_server.ps1"
$StartupDir = [Environment]::GetFolderPath("Startup")
$CmdPath = Join-Path $StartupDir "StockRlTraderWatchdog.cmd"

$Command = "@echo off`r`n"
$Command += "cd /d `"$Root`"`r`n"
$Command += "start `"StockRlTraderWatchdog`" powershell.exe -WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -File `"$WatchScript`" -Port $Port -IntervalSeconds $IntervalSeconds`r`n"

Set-Content -Path $CmdPath -Value $Command -Encoding ASCII
Start-Process -FilePath "powershell" `
    -ArgumentList "-WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -File `"$WatchScript`" -Port $Port -IntervalSeconds $IntervalSeconds" `
    -WorkingDirectory $Root `
    -WindowStyle Hidden

Write-Output "Installed startup watchdog: $CmdPath"
