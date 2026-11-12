<#
.SYNOPSIS
  Stop StoreGuard and remove its "start at sign-in" scheduled task.
  Postgres keeps its data (the container is stopped, not deleted).
#>
param([string]$TaskName = "StoreGuard")

& (Join-Path $PSScriptRoot "stop.ps1") -TaskName $TaskName
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "Autostart removed. Run it by hand with: .\make.ps1 serve" -ForegroundColor Green
