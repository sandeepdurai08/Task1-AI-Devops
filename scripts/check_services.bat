@echo off
REM ─────────────────────────────────────────────────────────────────────────────
REM  scripts/check_services.bat
REM
REM  Pre-flight checks for BuildBot (Windows Batch version).
REM  Can be run standalone or called from start.bat.
REM
REM  Usage:
REM    scripts\check_services.bat           (from project root)
REM
REM  Exit codes:
REM    0  All required checks passed
REM    1  One or more REQUIRED checks failed
REM ─────────────────────────────────────────────────────────────────────────────

setlocal enabledelayedexpansion
set FAILED=0
set PROJECT_ROOT=%~dp0..

echo.
echo   BuildBot -- Pre-flight checks
echo   --------------------------------

REM ── Check 1: Python ──────────────────────────────────────────────────────────
echo.
echo   [Python]
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo   [XX] Python not found.
    echo        Install Python 3.11+ and add it to PATH.
    set FAILED=1
) else (
    for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo   [OK] %%v
)

REM ── Check 2: .env ────────────────────────────────────────────────────────────
echo.
echo   [.env configuration]
if not exist "%PROJECT_ROOT%\.env" (
    echo   [XX] .env not found.
    echo        Run:  copy .env.example .env
    echo        Then fill in JENKINS_USER and JENKINS_TOKEN.
    set FAILED=1
) else (
    echo   [OK] .env exists

    REM Check JENKINS_USER is not empty
    for /f "tokens=1,* delims==" %%a in ('findstr /i "^JENKINS_USER" "%PROJECT_ROOT%\.env"') do (
        set VAL=%%b
        if "!VAL!"=="" (
            echo   [!!] JENKINS_USER is empty
            set FAILED=1
        ) else (
            echo   [OK] JENKINS_USER is set
        )
    )

    REM Check JENKINS_TOKEN is not empty
    for /f "tokens=1,* delims==" %%a in ('findstr /i "^JENKINS_TOKEN" "%PROJECT_ROOT%\.env"') do (
        set VAL=%%b
        if "!VAL!"=="" (
            echo   [!!] JENKINS_TOKEN is empty
            set FAILED=1
        ) else (
            echo   [OK] JENKINS_TOKEN is set
        )
    )
)

REM ── Check 3: pip packages ────────────────────────────────────────────────────
echo.
echo   [Python packages]
pip show flask >nul 2>&1
if %errorlevel% neq 0 (
    echo   [!!] Flask not installed
    echo        Run:  pip install -r requirements.txt
) else (
    echo   [OK] Flask installed
)
pip show openai >nul 2>&1
if %errorlevel% neq 0 (
    echo   [!!] openai not installed
    echo        Run:  pip install -r requirements.txt
) else (
    echo   [OK] openai installed
)

REM ── Check 4: Jenkins ─────────────────────────────────────────────────────────
echo.
echo   [Jenkins - localhost:8080]
curl -s -o nul -w "%%{http_code}" http://localhost:8080/login --connect-timeout 4 > "%TEMP%\bb_jenkins.tmp" 2>nul
set /p JSTATUS=< "%TEMP%\bb_jenkins.tmp"
del "%TEMP%\bb_jenkins.tmp" >nul 2>&1
if "%JSTATUS%"=="200" (
    echo   [OK] Jenkins is running at http://localhost:8080
) else (
    echo   [!!] Jenkins is NOT running at http://localhost:8080
    echo        Start:  java -jar jenkins.war --httpPort=8080
    echo        Builds will fail until Jenkins is started.
)

REM ── Check 5: MailHog ─────────────────────────────────────────────────────────
echo.
echo   [MailHog - localhost:8025]
curl -s -o nul -w "%%{http_code}" http://localhost:8025 --connect-timeout 3 > "%TEMP%\bb_mailhog.tmp" 2>nul
set /p MSTATUS=< "%TEMP%\bb_mailhog.tmp"
del "%TEMP%\bb_mailhog.tmp" >nul 2>&1
if "%MSTATUS%"=="200" (
    echo   [OK] MailHog is running at http://localhost:8025
) else (
    echo   [!!] MailHog is NOT running
    echo        Start:  MailHog.exe
    echo        Email notifications will be skipped.
)

REM ── Summary ───────────────────────────────────────────────────────────────────
echo.
echo   --------------------------------
if %FAILED%==1 (
    echo   Result: REQUIRED checks failed -- fix before starting BuildBot.
) else (
    echo   Result: All required checks passed.
)
echo.

exit /b %FAILED%
