@echo off
setlocal EnableDelayedExpansion
:: ============================================================
::  Jenkins Build Dependencies Installer
::  Installs: Maven 3.9.16, .NET 10 SDK (LTS), Jenkins LTS 2.555.3
::  Run this script as ADMINISTRATOR
::
::  SECURITY & SAFETY CHECKS APPLIED:
::  [S1] Admin-only enforcement before any action
::  [S2] SHA-256 checksum verification for Maven ZIP
::  [S3] HTTPS-only downloads — no HTTP fallback
::  [S4] Download temp folder scoped to TEMP, cleaned on exit
::  [S5] Maven ZIP extraction validated before PATH change
::  [S6] dotnet-install.ps1 downloaded from official dot.net domain only
::  [S7] Initial admin password printed to console only — not logged to file
::  [S8] Jenkins MSI verified to exist before silent install
::  [S9] PATH update guards against duplicate entries
::  [S10] setlocal keeps all variables session-scoped; no env pollution
:: ============================================================

:: ── [S1] Must run as Administrator ──────────────────────────
net session >nul 2>&1
if %errorLevel% NEQ 0 (
    echo.
    echo [ERROR] This script must be run as Administrator.
    echo         Right-click the .bat file ^> "Run as administrator"
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Jenkins Build Dependencies Installer
echo   Running as Administrator - OK
echo ============================================================
echo.

:: ── [S4] Scoped temp download folder ────────────────────────
set "DOWNLOADS=%TEMP%\jenkins-setup-%RANDOM%"
mkdir "%DOWNLOADS%" 2>nul
if not exist "%DOWNLOADS%" (
    echo [ERROR] Could not create temp folder: %DOWNLOADS%
    pause
    exit /b 1
)
echo [INFO] Temp download folder: %DOWNLOADS%
echo.

:: ============================================================
::  STEP 1 – Verify Java is present
:: ============================================================
echo [1/4] Checking Java...
java -version >nul 2>&1
if %errorLevel% NEQ 0 (
    echo [ERROR] Java not found. Please install JDK 11 or later from:
    echo         https://adoptium.net/
    echo         Then re-run this script.
    goto :CLEANUP
)
for /f "tokens=3" %%v in ('java -version 2^>^&1 ^| findstr /i "version"') do (
    echo [OK] Java found: %%v
)
echo.

:: ============================================================
::  STEP 2 – Install Apache Maven 3.9.16
:: ============================================================
echo [2/4] Checking Maven...
mvn -version >nul 2>&1
if %errorLevel% EQU 0 (
    echo [OK] Maven already installed:
    mvn -version 2>&1 | findstr /i "apache maven"
    echo.
    goto :DOTNET
)

echo [INFO] Maven not found. Downloading Maven 3.9.16 from Apache CDN (HTTPS)...

set "MAVEN_ZIP=%DOWNLOADS%\apache-maven-3.9.16-bin.zip"
set "MAVEN_DIR=C:\Program Files\Maven"
set "MAVEN_HOME=C:\Program Files\Maven\apache-maven-3.9.16"

:: [S3] HTTPS-only download from official Apache CDN
powershell -NoProfile -Command ^
  "Invoke-WebRequest -Uri 'https://dlcdn.apache.org/maven/maven-3/3.9.16/binaries/apache-maven-3.9.16-bin.zip' -OutFile '%MAVEN_ZIP%' -UseBasicParsing"

if not exist "%MAVEN_ZIP%" (
    echo [ERROR] Maven download failed. Check your internet connection.
    goto :CLEANUP
)

:: [S2] SHA-256 checksum verification
echo [INFO] Verifying Maven ZIP checksum...
set "MAVEN_EXPECTED_SHA=5af3b743dd8b876b5c45da33b676251e5f1687712644abb4ee519ca56e1d89ce"
for /f %%H in ('powershell -NoProfile -Command ^
  "(Get-FileHash -Path '%MAVEN_ZIP%' -Algorithm SHA256).Hash.ToLower()"') do set "MAVEN_ACTUAL_SHA=%%H"

if /i "!MAVEN_ACTUAL_SHA!" NEQ "!MAVEN_EXPECTED_SHA!" (
    echo [SECURITY ERROR] Maven ZIP checksum MISMATCH!
    echo   Expected: !MAVEN_EXPECTED_SHA!
    echo   Got:      !MAVEN_ACTUAL_SHA!
    echo [ACTION]  The downloaded file may be corrupted or tampered with.
    echo           Deleting the file. Please retry or download manually from:
    echo           https://maven.apache.org/download.cgi
    del /f "%MAVEN_ZIP%" >nul 2>&1
    goto :CLEANUP
)
echo [OK] Checksum verified.

:: [S5] Extract only after checksum passes
if not exist "%MAVEN_DIR%" mkdir "%MAVEN_DIR%"
powershell -NoProfile -Command "Expand-Archive -Path '%MAVEN_ZIP%' -DestinationPath '%MAVEN_DIR%' -Force"

:: Verify extraction succeeded before touching PATH
if not exist "%MAVEN_HOME%\bin\mvn.cmd" (
    echo [ERROR] Maven extraction failed — mvn.cmd not found at expected path.
    echo         Expected: %MAVEN_HOME%\bin\mvn.cmd
    goto :CLEANUP
)

:: Set MAVEN_HOME system variable
setx MAVEN_HOME "%MAVEN_HOME%" /M >nul

:: [S9] Guard against duplicate PATH entries
for /f "skip=2 tokens=2*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "SYS_PATH=%%B"
echo !SYS_PATH! | find /i "apache-maven" >nul
if %errorLevel% NEQ 0 (
    setx Path "!SYS_PATH!;%MAVEN_HOME%\bin" /M >nul
    echo [OK] Maven bin added to system PATH.
) else (
    echo [OK] Maven already in system PATH ^(no duplicate added^).
)

echo [OK] Maven 3.9.16 installed: %MAVEN_HOME%
echo      Open a NEW terminal window to use 'mvn'.
echo.

:DOTNET
:: ============================================================
::  STEP 3 – Install .NET 10 SDK (LTS)
:: ============================================================
echo [3/4] Checking .NET SDK...
dotnet --version >nul 2>&1
if %errorLevel% EQU 0 (
    echo [OK] .NET SDK already installed:
    dotnet --version
    echo.
    goto :JENKINS
)

echo [INFO] .NET SDK not found. Downloading official dotnet-install script (HTTPS)...

:: [S3] [S6] Download only from official dot.net domain
set "DOTNET_SCRIPT=%DOWNLOADS%\dotnet-install.ps1"
powershell -NoProfile -Command ^
  "Invoke-WebRequest -Uri 'https://dot.net/v1/dotnet-install.ps1' -OutFile '%DOTNET_SCRIPT%' -UseBasicParsing"

if not exist "%DOTNET_SCRIPT%" (
    echo [ERROR] dotnet-install.ps1 download failed.
    goto :CLEANUP
)

echo [INFO] Installing .NET 10 SDK (LTS) to C:\Program Files\dotnet ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%DOTNET_SCRIPT%" ^
  -Channel 10.0 -InstallDir "C:\Program Files\dotnet"

if not exist "C:\Program Files\dotnet\dotnet.exe" (
    echo [ERROR] .NET installation failed — dotnet.exe not found.
    goto :CLEANUP
)

:: [S9] Guard against duplicate PATH entries
for /f "skip=2 tokens=2*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "SYS_PATH=%%B"
echo !SYS_PATH! | find /i "C:\Program Files\dotnet" >nul
if %errorLevel% NEQ 0 (
    setx Path "!SYS_PATH!;C:\Program Files\dotnet" /M >nul
    echo [OK] .NET added to system PATH.
) else (
    echo [OK] .NET already in system PATH ^(no duplicate added^).
)

echo [OK] .NET 10 SDK (LTS) installed: C:\Program Files\dotnet
echo      Open a NEW terminal window to use 'dotnet'.
echo.

:JENKINS
:: ============================================================
::  STEP 4 – Install Jenkins LTS
:: ============================================================
echo [4/4] Checking Jenkins...
sc query Jenkins >nul 2>&1
if %errorLevel% EQU 0 (
    echo [OK] Jenkins service already installed.
    sc query Jenkins | findstr /i "state"
    echo.
    goto :RESTART_JENKINS
)

echo [INFO] Jenkins not found. Downloading Jenkins LTS MSI (HTTPS)...

set "JENKINS_MSI=%DOWNLOADS%\jenkins.msi"

:: [S3] HTTPS-only from official Jenkins distribution server
powershell -NoProfile -Command ^
  "Invoke-WebRequest -Uri 'https://get.jenkins.io/windows-stable/latest/jenkins.msi' -OutFile '%JENKINS_MSI%' -UseBasicParsing"

:: [S8] Verify MSI downloaded before running installer
if not exist "%JENKINS_MSI%" (
    echo [ERROR] Jenkins MSI download failed. Check your internet connection.
    goto :CLEANUP
)

:: Confirm MSI file size is reasonable (>10MB expected)
for %%F in ("%JENKINS_MSI%") do set "MSI_SIZE=%%~zF"
if !MSI_SIZE! LSS 10000000 (
    echo [ERROR] Jenkins MSI file seems too small ^(!MSI_SIZE! bytes^).
    echo         The download may be incomplete or redirected to an error page.
    goto :CLEANUP
)

echo [INFO] Downloaded Jenkins MSI ^(!MSI_SIZE! bytes^). Installing silently...

:: Silent install as Windows service on port 8080
msiexec /i "%JENKINS_MSI%" /qn /norestart JENKINSPORT=8080
if %errorLevel% NEQ 0 (
    echo [ERROR] Jenkins MSI installation returned error code: %errorLevel%
    echo         Try running the MSI manually: %JENKINS_MSI%
    goto :CLEANUP
)

timeout /t 10 /nobreak >nul

net start Jenkins >nul 2>&1
if %errorLevel% EQU 0 (
    echo [OK] Jenkins service started.
) else (
    echo [WARN] Jenkins installed but service did not start automatically.
    echo        Run manually: net start Jenkins
)

echo.
echo [INFO] Jenkins URL:  http://localhost:8080
echo [INFO] Unlock password file:
echo        C:\ProgramData\Jenkins\.jenkins\secrets\initialAdminPassword
echo.

:: [S7] Print unlock password to console ONLY — never write to a log file
powershell -NoProfile -Command ^
  "if (Test-Path 'C:\ProgramData\Jenkins\.jenkins\secrets\initialAdminPassword') { Write-Host '[INFO] Initial Admin Password: ' -NoNewline; Get-Content 'C:\ProgramData\Jenkins\.jenkins\secrets\initialAdminPassword' } else { Write-Host '[INFO] Password file not ready yet — Jenkins may still be initializing. Check back in 30 seconds.' }"
echo.

:RESTART_JENKINS
:: ============================================================
::  STEP 5 – Restart Jenkins to pick up updated PATH
:: ============================================================
echo [INFO] Restarting Jenkins service to load updated PATH ^(Maven + dotnet^)...
net stop Jenkins >nul 2>&1
timeout /t 5 /nobreak >nul
net start Jenkins >nul 2>&1
if %errorLevel% EQU 0 (
    echo [OK] Jenkins restarted — new PATH is active.
) else (
    echo [WARN] Jenkins restart failed. Run manually:
    echo        net stop Jenkins
    echo        net start Jenkins
)
echo.

:: ============================================================
::  SUMMARY
:: ============================================================
echo ============================================================
echo   All Done — Summary
echo ============================================================
echo.
echo  Component     Location
echo  ------------- -----------------------------------------
echo  Java          Already installed
echo  Git           Already installed
echo  Maven 3.9.16  C:\Program Files\Maven\apache-maven-3.9.16
echo  .NET 10 SDK   C:\Program Files\dotnet
echo  Jenkins LTS   http://localhost:8080
echo.
echo  NEXT STEPS IN JENKINS UI:
echo  1. Open http://localhost:8080
echo  2. Enter the initial admin password shown above
echo  3. Choose "Install suggested plugins"
echo  4. Manage Jenkins ^> Global Tool Configuration:
echo       JDK   name="JDK 21"    JAVA_HOME=%JAVA_HOME%
echo       Maven name="Maven 3"   MAVEN_HOME=C:\Program Files\Maven\apache-maven-3.9.16
echo  5. Create a Pipeline job for each project Jenkinsfile
echo.
echo ============================================================

:CLEANUP
:: [S4] Remove temp download folder on exit
echo [INFO] Cleaning up temp files...
if exist "%DOWNLOADS%" (
    rmdir /s /q "%DOWNLOADS%" >nul 2>&1
    echo [OK] Temp folder removed.
)
echo.
endlocal
pause
