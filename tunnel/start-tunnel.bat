@echo off
setlocal
title Cloudflare Quick Tunnel - SDLC Orchestrator
cd /d "%~dp0"

REM --- 0. Origin port ------------------------------------------------------
REM Default 8000 = the local `make dev` uvicorn listener.
REM Pass a port to expose something else. The docker-compose app container is
REM published on the host as 127.0.0.1:8010 (see `docker compose ps`), so:
REM     start-tunnel.bat 8010
if "%~1"=="" (set "PORT=8000") else (set "PORT=%~1")

echo ============================================
echo   Cloudflare Quick Tunnel Launcher
echo   Origin target: http://localhost:%PORT%
echo ============================================
echo.

REM --- 1. Ensure a WORKING cloudflared.exe (auto-download / auto-repair) ---
REM A partially-downloaded cloudflared.exe is ~2.6 MB instead of ~47 MB and
REM still "exists", so the old check `if not exist` would happily run it and
REM die with "[WinError 193] %1 is not a valid Win32 application". We therefore
REM validate the binary by executing `--version`, and we always download to a
REM temp name first so a failed download can never leave a broken exe in place.
set "CF_DL=https://gh-proxy.com/https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"

call :check_cf
if not errorlevel 1 goto :cf_ok

if exist "cloudflared.exe" (
    echo [!] cloudflared.exe exists but is NOT runnable ^(partial/corrupt download^).
    echo [*] Removing the broken copy...
    del /f /q "cloudflared.exe"
)

echo [!] Downloading cloudflared.exe from mirror...
if exist "cloudflared.tmp.exe" del /f /q "cloudflared.tmp.exe"
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri '%CF_DL%' -OutFile 'cloudflared.tmp.exe' -UseBasicParsing -TimeoutSec 600 } catch { Write-Host '[FAIL]' $_.Exception.Message; exit 1 }"
if errorlevel 1 (
    echo [X] Download failed. Check network/proxy, then re-run.
    if exist "cloudflared.tmp.exe" del /f /q "cloudflared.tmp.exe"
    pause
    exit /b 1
)

REM Validate the fresh download BEFORE trusting it.
"cloudflared.tmp.exe" --version >nul 2>&1
if errorlevel 1 (
    echo [X] Downloaded file is not a valid cloudflared binary ^(truncated^).
    echo     Download it manually and save as:
    echo       %~dp0cloudflared.exe
    echo     From: https://github.com/cloudflare/cloudflared/releases/latest
    echo           ^(file: cloudflared-windows-amd64.exe^)
    if exist "cloudflared.tmp.exe" del /f /q "cloudflared.tmp.exe"
    pause
    exit /b 1
)
move /y "cloudflared.tmp.exe" "cloudflared.exe" >nul

call :check_cf
if errorlevel 1 (
    echo [X] cloudflared.exe still not runnable after install. Manual fix required.
    pause
    exit /b 1
)
echo [OK] cloudflared.exe is ready.

:cf_ok

REM --- 2. If a tunnel is already running, ask before replacing it ---
tasklist /fi "imagename eq cloudflared.exe" 2>nul | find /i "cloudflared.exe" >nul
if %errorlevel%==0 (
    echo [!] A cloudflared tunnel is already running.
    set /p ANS="    Restart it with a NEW public URL? (y/N): "
    if /i not "%ANS%"=="y" (
        echo [*] Keeping existing tunnel. Exiting.
        timeout /t 3 >nul
        exit /b 0
    )
    echo [*] Stopping existing tunnel...
    taskkill /f /im cloudflared.exe >nul 2>&1
    timeout /t 2 >nul
)

REM --- 3. Check the SDLC app is reachable ---
echo [*] Checking app health at http://localhost:%PORT%/health ...
powershell -NoProfile -Command "try { $r=Invoke-WebRequest -Uri 'http://localhost:%PORT%/health' -UseBasicParsing -TimeoutSec 5; if($r.StatusCode -eq 200){Write-Host '[OK] App is running'}else{Write-Host '[!] App returned HTTP '$r.StatusCode; exit 1} } catch { Write-Host '[X] App NOT reachable at localhost:%PORT%.'; Write-Host '    Start Docker first:'; Write-Host '      cd /d c:\Users\13682\Desktop\fastapi-langgraph-agent-production-ready-template-master'; Write-Host '      docker compose --env-file .env.production up -d db app'; exit 1 }"
if errorlevel 1 (
    echo.
    pause
    exit /b 1
)

REM --- 4. Launch the quick tunnel (auto-protocol + IPv4 edge) ---
echo.
echo [*] Starting tunnel  (auto-protocol, edge: IPv4) ...
echo [*] Your public URL appears below as  https://*.trycloudflare.com
echo [*] KEEP THIS WINDOW OPEN to keep the tunnel alive.
echo [*] Close this window (or Ctrl-C) to stop the tunnel.
echo ============================================
echo.
REM Auto-protocol: let cloudflared pick QUIC or HTTP/2 based on precheck.
REM Network conditions on this host fluctuate — sometimes QUIC works, sometimes
REM HTTP/2. Forcing either one causes intermittent 530 errors. Auto-negotiation
REM is more resilient. --edge-ip-version 4 still forces IPv4 edge (IPv6 is
REM unreliable on this network and causes "connection forcibly closed" errors).
REM
REM Do NOT bother with --ha-connections here: in quick-tunnel mode (--url)
REM cloudflared silently forces it back to 1, and only one edge connection is
REM registered. Verified with `cloudflared tunnel --ha-connections 4 --url ...`
REM -> "Settings: map[... ha-connections:1 ...]" and a single connIndex=0.
REM
REM Intermittent 502/520/530 on larger static files (/, /js/*) is expected on
REM this host: the local origin answers 24/24 requests cleanly, so the loss is
REM on the host<->Cloudflare edge path. Just refresh the browser.
cloudflared.exe tunnel --no-autoupdate --edge-ip-version 4 --url http://localhost:%PORT%

echo.
echo [*] Tunnel has stopped.
pause

exit /b 0

REM --- helpers -------------------------------------------------------------
REM Returns errorlevel 0 only if cloudflared.exe exists AND actually executes.
:check_cf
if not exist "cloudflared.exe" exit /b 1
"cloudflared.exe" --version >nul 2>&1
if errorlevel 1 exit /b 1
exit /b 0
