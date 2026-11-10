<#
.SYNOPSIS
  StoreGuard - common tasks (Windows equivalent of the Makefile).

.EXAMPLE
  .\make.ps1 serve
  .\make.ps1 frontend-dev
  .\make.ps1 gpu-check
  .\make.ps1 help

.NOTES
  If Windows blocks this script from running ("running scripts is disabled
  on this system"), either run it once via:
    powershell -ExecutionPolicy Bypass -File make.ps1 <target>
  or allow local scripts for your user permanently:
    Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
#>
param(
    [Parameter(Position = 0)]
    [string]$Target = "help",

    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8765,
    [int]$CloudPort = 8000,
    [string]$Device = "auto",
    [string]$Config = "configs\example.yaml",
    [string]$Data = "data"
)

$CloudFrontend = "src\storeguard\cloud\frontend"
$DashboardFrontend = "src\storeguard\dashboard\frontend"

function Test-LastExit {
    param([string]$What)
    if ($LASTEXITCODE -ne 0) {
        Write-Host "$What failed (exit $LASTEXITCODE)" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

function Show-Help {
    Write-Host "StoreGuard targets (Windows):"
    Write-Host "  .\make.ps1 install                  sync Python deps (uv sync)"
    Write-Host "  .\make.ps1 data                      create data\ folder for videos"
    Write-Host "  .\make.ps1 serve                     control panel + live detection, one port: http://${BindHost}:${CloudPort}  (live view at /live/)"
    Write-Host "  .\make.ps1 frontend-dev              control panel UI with hot reload at http://localhost:5173 (run serve too)"
    Write-Host "  .\make.ps1 dashboard                 standalone detection UI only at http://${BindHost}:${Port}"
    Write-Host "  .\make.ps1 cloud                     control plane only, no detection, at http://${BindHost}:${CloudPort}"
    Write-Host "  .\make.ps1 frontend-build            rebuild both Vue frontends (cloud cabinet + dashboard)"
    Write-Host "  .\make.ps1 cloud-frontend-build      rebuild only the cloud cabinet's Vue frontend"
    Write-Host "  .\make.ps1 dashboard-frontend-build  rebuild only the dashboard's Vue frontend"
    Write-Host "  .\make.ps1 run                       run pipeline (Config=$Config)"
    Write-Host "  .\make.ps1 run-show                  run pipeline with preview windows"
    Write-Host "  .\make.ps1 gpu-check                 check (and fix) that detection runs on the NVIDIA GPU"
    Write-Host "  .\make.ps1 db-up                     start Postgres in Docker (data kept in a volume)"
    Write-Host "  .\make.ps1 db-down                   stop Postgres (data stays)"
    Write-Host "  .\make.ps1 migrate                   apply database migrations"
    Write-Host "  .\make.ps1 optimize                  build the TensorRT engine (NVIDIA) and show the speed-up"
    Write-Host "  .\make.ps1 test                      run pytest"
    Write-Host "  .\make.ps1 clean                     remove __pycache__ / pytest cache"
    Write-Host ""
    Write-Host "Normal use: .\make.ps1 serve, then open http://${BindHost}:${CloudPort}"
    Write-Host "Changed a frontend? .\make.ps1 frontend-build, then restart .\make.ps1 serve"
    Write-Host "Overrides: -BindHost -Port -CloudPort -Device -Config -Data"
}

function Invoke-Install {
    # With an NVIDIA GPU also install TensorRT (2-3x faster detection).
    # Plain `uv sync` would uninstall it again, so always ask for it here.
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
        uv sync --extra tensorrt
        if ($LASTEXITCODE -eq 0) { return }
        Write-Host "TensorRT could not be installed - continuing without it (detection still uses the GPU)." -ForegroundColor Yellow
    }
    uv sync
    Test-LastExit "uv sync"
}

function Invoke-Optimize {
    Invoke-Install
    uv run storeguard optimize --benchmark --device $Device
}

function Invoke-Data {
    New-Item -ItemType Directory -Force -Path $Data | Out-Null
    Write-Host "Drop .mp4 files into $Data\ then run: .\make.ps1 dashboard"
}

function Invoke-GpuCheck {
    # Detection on Windows needs the CUDA build of torch. pyproject.toml pulls
    # it from the PyTorch index, but a venv created before that change (or a
    # manual `pip install torch`) still holds the CPU-only build - fix that.
    uv run python scripts\gpu_check.py
    $code = $LASTEXITCODE
    if ($code -eq 2) {
        Write-Host "Re-installing the CUDA build of torch (one-time, ~3 GB)..." -ForegroundColor Yellow
        uv sync --reinstall-package torch --reinstall-package torchvision
        Test-LastExit "uv sync (torch CUDA reinstall)"
        uv run python scripts\gpu_check.py
        $code = $LASTEXITCODE
    }
    if ($code -eq 2 -or $code -eq 3) {
        Write-Host "GPU is NOT used - detection will run on CPU (slow). See the message above." -ForegroundColor Red
    }
}

function Invoke-DbUp {
    # Postgres in Docker (only the database; the app runs natively for the GPU).
    docker compose up -d --wait db
    Test-LastExit "docker compose up db (is Docker Desktop running?)"
    Write-Host "-> Postgres on 127.0.0.1:5432 (db/user: storeguard)"
}

function Invoke-Migrate {
    uv run alembic upgrade head
    Test-LastExit "alembic upgrade head"
}

function Invoke-Serve {
    Invoke-Install
    Invoke-DbUp
    Invoke-Migrate
    Invoke-GpuCheck
    Invoke-Data
    Write-Host "-> http://${BindHost}:${CloudPort}  (control panel; live detection at /live/, videos from $Data\)"
    uv run storeguard cloud --host $BindHost --port $CloudPort --with-dashboard --device $Device --data $Data --config $Config
}

function Invoke-FrontendDev {
    Write-Host "-> http://localhost:5173  (API calls go to .\make.ps1 serve on :${CloudPort})"
    Push-Location $CloudFrontend
    try {
        npm install
        Test-LastExit "npm install"
        npm run dev
    } finally {
        Pop-Location
    }
}

function Invoke-Dashboard {
    Invoke-Install
    Invoke-GpuCheck
    Invoke-Data
    Write-Host "-> http://${BindHost}:${Port}  (videos from $Data\)"
    uv run storeguard dashboard --host $BindHost --port $Port --device $Device --data $Data --config $Config
}

function Invoke-Cloud {
    Invoke-Install
    Invoke-DbUp
    Invoke-Migrate
    Write-Host "-> http://${BindHost}:${CloudPort}  (control panel only, no detection)"
    uv run storeguard cloud --host $BindHost --port $CloudPort
}

function Build-Frontend {
    param([string]$Dir)
    Push-Location $Dir
    try {
        npm install
        Test-LastExit "npm install ($Dir)"
        npm run build
        Test-LastExit "npm run build ($Dir)"
    } finally {
        Pop-Location
    }
}

function Invoke-CloudFrontendBuild { Build-Frontend $CloudFrontend }
function Invoke-DashboardFrontendBuild { Build-Frontend $DashboardFrontend }

function Invoke-FrontendBuild {
    Invoke-CloudFrontendBuild
    Invoke-DashboardFrontendBuild
}

function Invoke-Run {
    Invoke-Install
    Invoke-GpuCheck
    uv run storeguard run --config $Config
}

function Invoke-RunShow {
    Invoke-Install
    Invoke-GpuCheck
    uv run storeguard run --config $Config --show
}

function Invoke-Test {
    Invoke-Install
    uv run pytest -q
}

function Invoke-Clean {
    Remove-Item -Recurse -Force ".pytest_cache" -ErrorAction SilentlyContinue
    Get-ChildItem -Path . -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notlike "*\.venv\*" } |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}

switch ($Target) {
    "help" { Show-Help }
    "install" { Invoke-Install }
    "sync" { Invoke-Install }
    "data" { Invoke-Data }
    "serve" { Invoke-Serve }
    "frontend-dev" { Invoke-FrontendDev }
    "dashboard" { Invoke-Dashboard }
    "cloud" { Invoke-Cloud }
    "frontend-build" { Invoke-FrontendBuild }
    "cloud-frontend-build" { Invoke-CloudFrontendBuild }
    "dashboard-frontend-build" { Invoke-DashboardFrontendBuild }
    "run" { Invoke-Run }
    "run-show" { Invoke-RunShow }
    "gpu-check" { Invoke-Install; Invoke-GpuCheck }
    "db-up" { Invoke-DbUp }
    "db-down" { docker compose down }
    "db-logs" { docker compose logs -f db }
    "db-shell" { docker compose exec db psql -U storeguard -d storeguard }
    "migrate" { Invoke-Install; Invoke-Migrate }
    "optimize" { Invoke-Optimize }
    "test" { Invoke-Test }
    "clean" { Invoke-Clean }
    default {
        Write-Host "Unknown target: $Target" -ForegroundColor Red
        Show-Help
        exit 1
    }
}
