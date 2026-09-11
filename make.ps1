<#
.SYNOPSIS
  StoreGuard — common tasks (Windows equivalent of the Makefile).

.EXAMPLE
  .\make.ps1 dashboard
  .\make.ps1 cloud
  .\make.ps1 docker-up
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
    Write-Host "  .\make.ps1 dashboard                 open detection UI at http://${BindHost}:${Port}"
    Write-Host "  .\make.ps1 cloud                     run the cloud control plane at http://${BindHost}:${CloudPort} (--dev, local sqlite)"
    Write-Host "  .\make.ps1 frontend-build            rebuild both Vue frontends (cloud cabinet + dashboard)"
    Write-Host "  .\make.ps1 cloud-frontend-build      rebuild only the cloud cabinet's Vue frontend"
    Write-Host "  .\make.ps1 dashboard-frontend-build  rebuild only the dashboard's Vue frontend"
    Write-Host "  .\make.ps1 run                       run pipeline (Config=$Config)"
    Write-Host "  .\make.ps1 run-show                  run pipeline with preview windows"
    Write-Host "  .\make.ps1 test                      run pytest"
    Write-Host "  .\make.ps1 clean                     remove __pycache__ / pytest cache"
    Write-Host "  .\make.ps1 docker-build              build the cloud + dashboard Docker images"
    Write-Host "  .\make.ps1 docker-up                 build and start cloud + dashboard in Docker (background)"
    Write-Host "  .\make.ps1 docker-down               stop the Docker services"
    Write-Host "  .\make.ps1 docker-logs               follow logs from the Docker services"
    Write-Host "  .\make.ps1 docker-agent              build and start the headless edge agent in Docker"
    Write-Host ""
    Write-Host "Put videos in $Data\ then: .\make.ps1 dashboard"
    Write-Host "Changed a frontend? .\make.ps1 frontend-build, then .\make.ps1 cloud / dashboard"
    Write-Host "Note: 'Live view' in the cabinet proxies to the dashboard at http://${BindHost}:${Port} — start both to use it"
    Write-Host "Overrides: -BindHost -Port -CloudPort -Device -Config -Data"
}

function Invoke-Install {
    uv sync
    Test-LastExit "uv sync"
}

function Invoke-Data {
    New-Item -ItemType Directory -Force -Path $Data | Out-Null
    Write-Host "Drop .mp4 files into $Data\ then run: .\make.ps1 dashboard"
}

function Invoke-Dashboard {
    Invoke-Install
    Invoke-Data
    Write-Host "-> http://${BindHost}:${Port}  (videos from $Data\)"
    uv run storeguard dashboard --host $BindHost --port $Port --device $Device --data $Data --config $Config
}

function Invoke-Cloud {
    Invoke-Install
    Write-Host "-> http://${BindHost}:${CloudPort}  (--dev: local sqlite, tables created on startup)"
    uv run storeguard cloud --host $BindHost --port $CloudPort --dev
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
    uv run storeguard run --config $Config
}

function Invoke-RunShow {
    Invoke-Install
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

function Invoke-DockerBuild {
    docker compose build
    Test-LastExit "docker compose build"
}

function Invoke-DockerUp {
    docker compose up -d --build
    Test-LastExit "docker compose up"
    Write-Host "-> cloud: http://${BindHost}:${CloudPort}   dashboard: http://${BindHost}:${Port}"
    Write-Host "GPU not showing up? docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi"
}

function Invoke-DockerDown {
    docker compose down
}

function Invoke-DockerLogs {
    docker compose logs -f
}

function Invoke-DockerAgent {
    docker compose --profile agent up -d --build agent
    Test-LastExit "docker compose --profile agent up"
}

switch ($Target) {
    "help" { Show-Help }
    "install" { Invoke-Install }
    "sync" { Invoke-Install }
    "data" { Invoke-Data }
    "dashboard" { Invoke-Dashboard }
    "cloud" { Invoke-Cloud }
    "frontend-build" { Invoke-FrontendBuild }
    "cloud-frontend-build" { Invoke-CloudFrontendBuild }
    "dashboard-frontend-build" { Invoke-DashboardFrontendBuild }
    "run" { Invoke-Run }
    "run-show" { Invoke-RunShow }
    "test" { Invoke-Test }
    "clean" { Invoke-Clean }
    "docker-build" { Invoke-DockerBuild }
    "docker-up" { Invoke-DockerUp }
    "docker-down" { Invoke-DockerDown }
    "docker-logs" { Invoke-DockerLogs }
    "docker-agent" { Invoke-DockerAgent }
    default {
        Write-Host "Unknown target: $Target" -ForegroundColor Red
        Show-Help
        exit 1
    }
}
