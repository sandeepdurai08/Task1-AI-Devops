@echo off
REM ─────────────────────────────────────────────────────────────────────────────
REM  BuildBot — Startup Script (Windows Batch)
REM  Double-click or run from the project root:  start.bat
REM
REM  Pre-flight checks live in:  scripts\check_services.bat
REM  Run them standalone:        scripts\check_services.bat
REM ─────────────────────────────────────────────────────────────────────────────

title BuildBot — Exterro DevOps AI
color 0B

echo.
echo   ==========================================
echo    BuildBot  ^|  Exterro DevOps AI Challenge
echo   ==========================================
echo.

REM ── Run pre-flight checks ─────────────────────────────────────────────────────
if exist "scripts\check_services.bat" (
    call scripts\check_services.bat
    if %errorlevel% neq 0 (
        echo.
        echo   Aborting -- fix required issues above before starting.
        echo   Re-run checks:  scripts\check_services.bat
        echo.
        pause
        exit /b 1
    )
) else (
    echo   [!!] scripts\check_services.bat not found -- skipping checks
)

REM ── Activate virtual environment (if present) ─────────────────────────────────
if exist ".venv\Scripts\activate.bat" (
    echo   Activating .venv...
    call .venv\Scripts\activate.bat
)

REM ── Install / update packages ─────────────────────────────────────────────────
if exist "requirements.txt" (
    pip install -r requirements.txt --quiet
)

REM ── Start Flask ───────────────────────────────────────────────────────────────
echo.
echo   ──────────────────────────────────────────────
echo   Starting BuildBot on http://localhost:5000
echo   Press Ctrl+C to stop.
echo   ──────────────────────────────────────────────
echo.

REM Open browser after 2 seconds (non-blocking)
start "" /b cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:5000"

python app.py
pause
