@echo off
setlocal EnableDelayedExpansion

:: ============================================================
::  Maven 3.9.16 Setup Script
::  Uses the ZIP you already downloaded
::  RUN AS ADMINISTRATOR
:: ============================================================

:: [1] Admin check
net session >nul 2>&1
if %errorLevel% NEQ 0 (
    echo.
    echo [ERROR] Run this script as Administrator.
    echo         Right-click ^> "Run as administrator"
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Maven 3.9.16 Setup
echo ============================================================
echo.

:: [2] Confirm the ZIP exists
set "MAVEN_ZIP=%USERPROFILE%\Downloads\apache-maven-3.9.16-bin.zip"
if not exist "%MAVEN_ZIP%" (
    echo [ERROR] ZIP not found at: %MAVEN_ZIP%
    echo         Please check the file path and re-run.
    pause
    exit /b 1
)
echo [OK] Found ZIP: %MAVEN_ZIP%

:: [3] Verify file size (should be ~9MB)
for %%F in ("%MAVEN_ZIP%") do set "ZIP_SIZE=%%~zF"
if !ZIP_SIZE! LSS 5000000 (
    echo [ERROR] ZIP file too small (!ZIP_SIZE! bytes^) — may be corrupted.
    pause
    exit /b 1
)
echo [OK] File size OK: !ZIP_SIZE! bytes

:: [4] Extract to C:\Program Files\Maven
set "MAVEN_DIR=C:\Program Files\Maven"
set "MAVEN_HOME=C:\Program Files\Maven\apache-maven-3.9.16"

echo [INFO] Extracting to %MAVEN_DIR% ...
if not exist "%MAVEN_DIR%" mkdir "%MAVEN_DIR%"
powershell -NoProfile -Command "Expand-Archive -Path '%MAVEN_ZIP%' -DestinationPath '%MAVEN_DIR%' -Force"

:: [5] Verify extraction
if not exist "%MAVEN_HOME%\bin\mvn.cmd" (
    echo [ERROR] Extraction failed — mvn.cmd not found.
    echo         Expected: %MAVEN_HOME%\bin\mvn.cmd
    pause
    exit /b 1
)
echo [OK] Extracted successfully.

:: [6] Set MAVEN_HOME system environment variable
setx MAVEN_HOME "%MAVEN_HOME%" /M >nul
echo [OK] MAVEN_HOME set to: %MAVEN_HOME%

:: [7] Add Maven bin to system PATH (no duplicates)
for /f "skip=2 tokens=2*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "SYS_PATH=%%B"
echo !SYS_PATH! | find /i "apache-maven" >nul
if %errorLevel% NEQ 0 (
    setx Path "!SYS_PATH!;%MAVEN_HOME%\bin" /M >nul
    echo [OK] Maven bin added to system PATH.
) else (
    echo [OK] Maven already in PATH — no duplicate added.
)

echo.
echo ============================================================
echo   Maven 3.9.16 installed successfully!
echo   Location : %MAVEN_HOME%
echo   MAVEN_HOME: set as system variable
echo   PATH      : updated
echo.
echo   IMPORTANT: Open a NEW PowerShell or CMD window, then run:
echo              mvn -version
echo ============================================================
echo.

endlocal
pause
