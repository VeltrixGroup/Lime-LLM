<#
.SYNOPSIS
  Run Лайм AI / StoreGuard in the background: Docker Desktop -> Postgres -> app.

.DESCRIPTION
  Started by the "StoreGuard" scheduled task (see install-autostart.ps1) when
  Windows signs in, with no window. It:
    1. starts Docker Desktop if it isn't running and waits for the engine,
    2. starts the Postgres container (docker compose up -d --wait db),
    3. applies database migrations (alembic upgrade head),
    4. runs the control panel + live detection on one port, and restarts it
       if it ever exits (crash, camera driver hiccup, ...).
  Everything is logged to logs\storeguard-YYYY-MM-DD.log (14 days kept).
  Create the file logs\STOP (or run `.\make.ps1 bg-stop`) to end it.

.PARAMETER BindHost
  127.0.0.1 = only this PC can open the panel. 0.0.0.0 = other PCs on the
  store network too (allow the port in Windows Firewall).
#>
param(
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8000,
    [string]$Device = "auto",
    [string]$Config = ""
)

$ErrorActionPreference = "Continue"
# Output goes to log files, not a console: without UTF-8 mode Python uses the
# legacy codepage and crashes on Cyrillic / arrows in the log messages.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$StopFile = Join-Path $LogDir "STOP"
# A fresh STOP means "bg-stop was just run": don't let the task's
# restart-on-failure bring the app back. An old one (e.g. from before a
# reboot) is ignored, so the PC still starts everything at sign-in.
if (Test-Path $StopFile) {
    if ((Get-Item $StopFile).LastWriteTime -gt (Get-Date).AddMinutes(-3)) { exit 0 }
    Remove-Item $StopFile -ErrorAction SilentlyContinue
}

function Log([string]$msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path (Join-Path $LogDir ("storeguard-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))) -Value $line -Encoding UTF8
}

# Only one copy at a time (the task can be started twice by hand).
$mutex = New-Object System.Threading.Mutex($false, "Global\StoreGuardService")
if (-not $mutex.WaitOne(0)) {
    Log "Another StoreGuard instance is already running - exiting."
    exit 0
}

# Keep 14 days of logs.
Get-ChildItem $LogDir -Filter "storeguard-*.log" -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } |
    Remove-Item -ErrorAction SilentlyContinue

# The task runs with a minimal PATH: find uv and docker explicitly.
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe" }
if (-not (Test-Path $uv)) { Log "uv not found ($uv). Install it: irm https://astral.sh/uv/install.ps1 | iex"; exit 1 }
$dockerDir = "C:\Program Files\Docker\Docker\resources\bin"
if (Test-Path $dockerDir) { $env:PATH = "$dockerDir;$env:PATH" }

function Test-Docker {
    docker info --format "{{.ServerVersion}}" *> $null
    return $LASTEXITCODE -eq 0
}

# 1. Docker Desktop
if (-not (Test-Docker)) {
    $desktop = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    if (Test-Path $desktop) {
        Log "Starting Docker Desktop..."
        Start-Process $desktop -WindowStyle Hidden
    } else {
        Log "Docker Desktop not found at '$desktop' - is it installed?"
    }
    $deadline = (Get-Date).AddMinutes(5)
    while (-not (Test-Docker)) {
        if ((Get-Date) -gt $deadline) { Log "Docker engine did not start within 5 minutes - giving up (the task retries)."; exit 1 }
        Start-Sleep -Seconds 5
    }
}
Log "Docker engine is up."

# 2. Postgres
for ($i = 1; $i -le 5; $i++) {
    docker compose up -d --wait db *>> (Join-Path $LogDir "docker.log")
    if ($LASTEXITCODE -eq 0) { break }
    Log "Postgres did not start (attempt $i/5), retrying in 10 s..."
    Start-Sleep -Seconds 10
}
if ($LASTEXITCODE -ne 0) { Log "Postgres failed to start - see logs\docker.log"; exit 1 }
Log "Postgres is up."

# 3. Migrations (no network needed: --no-sync uses the installed packages)
& $uv run --no-sync alembic upgrade head *>> (Join-Path $LogDir "migrate.log")
if ($LASTEXITCODE -ne 0) { Log "Database migration failed - see logs\migrate.log"; exit 1 }

# 4. The app, restarted whenever it exits
if (-not $Config) {
    $Config = if (Test-Path "configs\storeguard.yaml") { "configs\storeguard.yaml" } else { "configs\example.yaml" }
}
New-Item -ItemType Directory -Force -Path "data" | Out-Null
while (-not (Test-Path $StopFile)) {
    Log "Starting the app: http://${BindHost}:${Port} (config $Config, device $Device)"
    $appLog = Join-Path $LogDir ("app-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
    & $uv run --no-sync storeguard cloud --host $BindHost --port $Port --with-dashboard `
        --device $Device --data data --config $Config *>> $appLog
    $code = $LASTEXITCODE
    if (Test-Path $StopFile) { break }
    Log "The app exited (code $code) - restarting in 10 s. Details: $appLog"
    Start-Sleep -Seconds 10
}
Log "Stopped (logs\STOP found)."
Remove-Item $StopFile -ErrorAction SilentlyContinue
$mutex.ReleaseMutex()
