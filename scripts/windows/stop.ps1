<#
.SYNOPSIS
  Stop the background StoreGuard app (the scheduled task stays installed and
  starts it again at the next sign-in, or with `.\make.ps1 bg-start`).
#>
param([string]$TaskName = "StoreGuard", [switch]$KeepDatabase)

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
# Tell the service loop not to restart the app.
New-Item -ItemType File -Force -Path (Join-Path $LogDir "STOP") | Out-Null

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

# Stopping the task ends PowerShell but not always its children: end the app too.
Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'storeguard.exe' OR Name = 'uv.exe'" |
    Where-Object { $_.CommandLine -like "*storeguard*cloud*" -or $_.CommandLine -like "*storeguard-service.ps1*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
    Where-Object { $_.CommandLine -like "*storeguard-service.ps1*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

if (-not $KeepDatabase) {
    Push-Location $Root
    docker compose stop db 2>$null | Out-Null
    Pop-Location
}
Write-Host "StoreGuard stopped." -ForegroundColor Yellow
