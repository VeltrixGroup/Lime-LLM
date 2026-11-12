<#
.SYNOPSIS
  Make Лайм AI / StoreGuard start by itself in the background when Windows signs in.

.DESCRIPTION
  Registers a scheduled task "StoreGuard" for the current user that runs
  storeguard-service.ps1 hidden at sign-in, restarts it if it fails, and has
  no time limit. Also prepares the project once (uv sync, .env with a random
  session secret if missing) and starts the task right away.

  Docker Desktop runs inside a signed-in Windows session, so for "turn the
  PC on and everything starts" also enable automatic sign-in for this user
  (README, section 4.6).

.EXAMPLE
  .\make.ps1 autostart-install
  .\make.ps1 autostart-install -BindHost 0.0.0.0    # reachable from the store network
#>
param(
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8000,
    [string]$Device = "auto",
    [string]$TaskName = "StoreGuard"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Service = Join-Path $PSScriptRoot "storeguard-service.ps1"
Set-Location $Root

# 1. Dependencies, once (the background task itself never downloads anything).
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe" }
if (-not (Test-Path $uv)) { throw "uv is not installed. Run: powershell -ExecutionPolicy ByPass -c `"irm https://astral.sh/uv/install.ps1 | iex`"" }
Write-Host "Installing dependencies (uv sync)..."
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    & $uv sync --extra tensorrt
    if ($LASTEXITCODE -ne 0) { & $uv sync }
} else {
    & $uv sync
}
if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }

# 2. A real session secret (the default one is public and insecure).
$envFile = Join-Path $Root ".env"
$hasSecret = (Test-Path $envFile) -and (Select-String -Path $envFile -Pattern "^STOREGUARD_SECRET_KEY=" -Quiet)
if (-not $hasSecret) {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $secret = [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
    Add-Content -Path $envFile -Value "STOREGUARD_SECRET_KEY=$secret" -Encoding ASCII
    Write-Host "Created a session secret in .env"
}

# 3. The scheduled task.
$taskArgs = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Service`" -BindHost $BindHost -Port $Port -Device $Device"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $taskArgs -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Лайм AI: Docker (Postgres) + control panel + live detection" -Force | Out-Null

# 4. Start it now.
Remove-Item (Join-Path $Root "logs\STOP") -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName $TaskName
Write-Host ""
Write-Host "Done. StoreGuard now starts by itself every time '$env:USERNAME' signs in." -ForegroundColor Green
Write-Host "It is starting now in the background: http://${BindHost}:${Port}  (first start can take 1-2 minutes)"
Write-Host "Logs:   $Root\logs\"
Write-Host "Stop:   .\make.ps1 bg-stop      Start: .\make.ps1 bg-start      Remove: .\make.ps1 autostart-remove"
