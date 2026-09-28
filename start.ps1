# ─────────────────────────────────────────────────────────────────────────────
#  BuildBot — Startup Script (PowerShell)
#  Run from the project root:  .\start.ps1
#
#  Pre-flight checks live in:  scripts\check_services.ps1
#  Run them standalone at any time:  .\scripts\check_services.ps1
# ─────────────────────────────────────────────────────────────────────────────

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot

Clear-Host
Write-Host ""
Write-Host "  ██████╗ ██╗   ██╗██╗██╗     ██████╗ ██████╗  ██████╗ ████████╗" -ForegroundColor Magenta
Write-Host "  ██╔══██╗██║   ██║██║██║     ██╔══██╗██╔══██╗██╔═══██╗╚══██╔══╝" -ForegroundColor Magenta
Write-Host "  ██████╔╝██║   ██║██║██║     ██║  ██║██████╔╝██║   ██║   ██║   " -ForegroundColor Magenta
Write-Host "  ██╔══██╗██║   ██║██║██║     ██║  ██║██╔══██╗██║   ██║   ██║   " -ForegroundColor Magenta
Write-Host "  ██████╔╝╚██████╔╝██║███████╗██████╔╝██████╔╝╚██████╔╝   ██║   " -ForegroundColor Magenta
Write-Host "  ╚═════╝  ╚═════╝ ╚═╝╚══════╝╚═════╝ ╚═════╝  ╚═════╝   ╚═╝   " -ForegroundColor Magenta
Write-Host ""
Write-Host "  Exterro DevOps AI Challenge · September 2026" -ForegroundColor DarkGray
Write-Host ""

# ─── Run pre-flight checks ────────────────────────────────────────────────────
$checkScript = Join-Path $ProjectRoot "scripts\check_services.ps1"

if (Test-Path $checkScript) {
    & $checkScript
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  Aborting startup — fix the required issues above." -ForegroundColor Red
        Write-Host "  Re-run checks at any time:  .\scripts\check_services.ps1" -ForegroundColor DarkGray
        Write-Host ""
        exit 1
    }
} else {
    Write-Host "  [!!] Check script not found at scripts\check_services.ps1" -ForegroundColor Yellow
}

# ─── Activate virtual environment ────────────────────────────────────────────
$venvActivate = Join-Path $ProjectRoot ".venv\Scripts\Activate.ps1"
if (Test-Path $venvActivate) {
    Write-Host "  Activating .venv..." -ForegroundColor Cyan
    & $venvActivate
}

# ─── Install / update packages ───────────────────────────────────────────────
$reqFile = Join-Path $ProjectRoot "requirements.txt"
if (Test-Path $reqFile) {
    pip install -r $reqFile --quiet
}

# ─── Start Flask ─────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  ─────────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host "  Starting BuildBot → http://localhost:5000    " -ForegroundColor White
Write-Host "  Press Ctrl+C to stop.                        " -ForegroundColor DarkGray
Write-Host "  ─────────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host ""

# Open browser 2 seconds after Flask starts (non-blocking)
Start-Job -ScriptBlock {
    Start-Sleep -Seconds 2
    Start-Process "http://localhost:5000"
} | Out-Null

Set-Location $ProjectRoot
python app.py
