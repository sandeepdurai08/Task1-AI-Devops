# ─────────────────────────────────────────────────────────────────────────────
#  scripts/check_services.ps1
#
#  Pre-flight checks for BuildBot.
#  Can be run standalone or called from start.ps1.
#
#  Usage:
#    .\scripts\check_services.ps1                  # from project root
#    .\scripts\check_services.ps1 -Quiet           # suppress banner, exit code only
#
#  Exit codes:
#    0  All required checks passed (warnings allowed)
#    1  One or more REQUIRED checks failed
# ─────────────────────────────────────────────────────────────────────────────

param(
    [switch]$Quiet   # suppress decorative output; used when called from start.ps1
)

# Resolve project root — always two levels up from this script's location
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

# ─── Output helpers ───────────────────────────────────────────────────────────
function Write-OK   { param($msg) Write-Host "  [OK]  $msg" -ForegroundColor Green }
function Write-Warn { param($msg) Write-Host "  [!!]  $msg" -ForegroundColor Yellow }
function Write-Fail { param($msg) Write-Host "  [XX]  $msg" -ForegroundColor Red }
function Write-Info { param($msg) Write-Host "        $msg" -ForegroundColor Gray }
function Write-Head { param($msg) if (-not $Quiet) { Write-Host "`n  $msg" -ForegroundColor Cyan } }

$RequiredFailed = $false

if (-not $Quiet) {
    Write-Host ""
    Write-Host "  BuildBot — Pre-flight checks" -ForegroundColor White
    Write-Host "  ────────────────────────────" -ForegroundColor DarkGray
}

# ─── Check 1: Python ──────────────────────────────────────────────────────────
Write-Head "Python"
try {
    $v = python --version 2>&1
    Write-OK $v
} catch {
    Write-Fail "Python not found — install Python 3.11+ and add it to PATH."
    $RequiredFailed = $true
}

# ─── Check 2: .env file ───────────────────────────────────────────────────────
Write-Head ".env configuration"
$envFile = Join-Path $ProjectRoot ".env"

if (-not (Test-Path $envFile)) {
    Write-Fail ".env not found."
    Write-Info "Fix:  copy `"$ProjectRoot\.env.example`" `"$ProjectRoot\.env`""
    Write-Info "      Then fill in JENKINS_USER, JENKINS_TOKEN, etc."
    $RequiredFailed = $true
} else {
    Write-OK ".env exists"
    $envContent = Get-Content $envFile -Raw

    # Required keys — must be present and non-placeholder
    $required = [ordered]@{
        "JENKINS_USER"  = @{ placeholder = "your_jenkins_username";  label = "Jenkins username" }
        "JENKINS_TOKEN" = @{ placeholder = "your_jenkins_api_token"; label = "Jenkins API token" }
        "LLM_URL"       = @{ placeholder = "";                       label = "LLM endpoint URL" }
    }

    foreach ($key in $required.Keys) {
        if ($envContent -match "(?m)^$key\s*=\s*(.+)$") {
            $val = $matches[1].Trim()
            if ($val -eq "" -or $val -eq $required[$key].placeholder) {
                Write-Warn "$key not set — $($required[$key].label) is required"
                $RequiredFailed = $true
            } else {
                Write-OK "$key is set"
            }
        } else {
            Write-Warn "$key missing from .env"
            $RequiredFailed = $true
        }
    }

    # Optional keys — warn if missing but don't fail
    $optional = @{
        "GCHAT_WEBHOOK"  = "Google Chat notifications will be skipped"
        "JENKINS_JOBS"   = "All jobs visible to JENKINS_USER will be accessible (no allowlist)"
    }

    foreach ($key in $optional.Keys) {
        if ($envContent -match "(?m)^$key\s*=\s*$") {
            Write-Warn "$key is empty — $($optional[$key])"
        }
    }
}

# ─── Check 3: pip packages ────────────────────────────────────────────────────
Write-Head "Python packages"
$reqFile = Join-Path $ProjectRoot "requirements.txt"
if (Test-Path $reqFile) {
    try {
        $check = pip show flask 2>&1
        if ($check -match "Name: Flask") {
            Write-OK "Flask installed"
        } else {
            Write-Warn "Flask not found — run:  pip install -r requirements.txt"
        }
        $check2 = pip show openai 2>&1
        if ($check2 -match "Name: openai") {
            Write-OK "openai installed"
        } else {
            Write-Warn "openai not found — run:  pip install -r requirements.txt"
        }
    } catch {
        Write-Warn "Could not verify packages — run pip install -r requirements.txt manually"
    }
} else {
    Write-Warn "requirements.txt not found"
}

# ─── Check 4: Jenkins ─────────────────────────────────────────────────────────
Write-Head "Jenkins"
$jenkinsUrl = "http://localhost:8080"
if ($envContent -match "(?m)^JENKINS_URL\s*=\s*(.+)$") {
    $jenkinsUrl = $matches[1].Trim()
}

try {
    $resp = Invoke-WebRequest -Uri "$jenkinsUrl/login" -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
    Write-OK "Jenkins is reachable at $jenkinsUrl"
} catch {
    Write-Warn "Jenkins is NOT running at $jenkinsUrl"
    Write-Info "Start it:  java -jar jenkins.war --httpPort=8080"
    Write-Info "(BuildBot will warn in chat — builds will fail until Jenkins starts)"
}

# ─── Check 5: MailHog ─────────────────────────────────────────────────────────
Write-Head "MailHog (local SMTP)"
try {
    $resp = Invoke-WebRequest -Uri "http://localhost:8025" -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
    Write-OK "MailHog is running at http://localhost:8025"
} catch {
    Write-Warn "MailHog is NOT running"
    Write-Info "Start it:  .\MailHog.exe"
    Write-Info "Download:  https://github.com/mailhog/MailHog/releases/latest"
    Write-Info "(Email notifications will be skipped until MailHog starts)"
}

# ─── Check 6: LLM endpoint ────────────────────────────────────────────────────
Write-Head "LLM endpoint"
# Read LLM_URL from .env — strip path component to get base URL
$envFile = Join-Path $PSScriptRoot "..\\.env"
$llmUrl  = "http://localhost"   # fallback
if (Test-Path $envFile) {
    $llmLine = Get-Content $envFile | Where-Object { $_ -match "^LLM_URL\s*=" }
    if ($llmLine) {
        $rawUrl = ($llmLine -split "=", 2)[1].Trim()
        # Strip /v1/... path to get base host URL for reachability check
        if ($rawUrl -match "^(https?://[^/]+)") { $llmUrl = $Matches[1] }
    }
}
try {
    $resp = Invoke-WebRequest -Uri $llmUrl -TimeoutSec 6 -UseBasicParsing -ErrorAction Stop
    Write-OK "LLM endpoint is reachable ($llmUrl)"
} catch {
    Write-Warn "LLM endpoint unreachable: $llmUrl"
    Write-Info "Check LLM_URL in .env and network/VPN access"
}

# ─── Summary ──────────────────────────────────────────────────────────────────
if (-not $Quiet) {
    Write-Host ""
    if ($RequiredFailed) {
        Write-Host "  Result: REQUIRED checks failed — fix the issues above before starting." -ForegroundColor Red
    } else {
        Write-Host "  Result: All required checks passed." -ForegroundColor Green
    }
    Write-Host ""
}

if ($RequiredFailed) { exit 1 } else { exit 0 }
