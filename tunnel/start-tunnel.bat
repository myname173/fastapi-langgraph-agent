@echo off
title Cloudflare Quick Tunnel - SDLC Orchestrator
cd /d "%~dp0"

echo ============================================
echo   Cloudflare Quick Tunnel Launcher
echo   Origin target: http://localhost:8000
echo ============================================
echo.

REM --- 1. Ensure cloudflared.exe is present (auto-download if missing) ---
if not exist "cloudflared.exe" (
    echo [!] cloudflared.exe not found in this folder. Downloading from mirror...
    powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'https://gh-proxy.com/https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile 'cloudflared.exe' -UseBasicParsing -TimeoutSec 300; if (Test-Path 'cloudflared.exe') { Write-Host '[OK] Downloaded cloudflared.exe' } else { exit 1 } } catch { Write-Host '[FAIL]' $_.Exception.Message; exit 1 }"
    if not exist "cloudflared.exe" (
        echo [X] Download failed. Manually place cloudflared.exe in this folder, then re-run.
        echo     Direct URL: https://github.com/cloudflare/cloudflared/releases/latest
        pause
        exit /b 1
    )
)

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
echo [*] Checking app health at http://localhost:8000/health ...
powershell -NoProfile -Command "try { $r=Invoke-WebRequest -Uri 'http://localhost:8000/health' -UseBasicParsing -TimeoutSec 5; if($r.StatusCode -eq 200){Write-Host '[OK] App is running'}else{Write-Host '[!] App returned HTTP '$r.StatusCode; exit 1} } catch { Write-Host '[X] App NOT reachable at localhost:8000.'; Write-Host '    Start Docker first:'; Write-Host '      cd /d c:\Users\13682\Desktop\fastapi-langgraph-agent-production-ready-template-master'; Write-Host '      docker compose --env-file .env.production up -d db app'; exit 1 }"
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
cloudflared.exe tunnel --no-autoupdate --edge-ip-version 4 --url http://localhost:8000

echo.
echo [*] Tunnel has stopped.
pause
