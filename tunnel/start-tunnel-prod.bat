@echo off
setlocal
title Cloudflare Tunnel (production) - SDLC Orchestrator
cd /d "%~dp0"

REM ===========================================================================
REM One-click PRODUCTION launcher.
REM
REM   1. checks whether the app is already answering on 127.0.0.1:8010
REM   2. if not, runs `docker compose --env-file .env.production up -d db app`
REM      and waits for /health to turn green (up to 180s)
REM   3. hands off to start-tunnel.bat, which publishes a public
REM      https://*.trycloudflare.com URL pointing at that port
REM
REM Requires Docker Desktop to be running. Only db + app are started: the app
REM container depends on db alone, and the heavier optional services (vllm,
REM prometheus, grafana, cadvisor, mcp) are left untouched. Start those
REM yourself if you need them.
REM
REM NOTE: this file must keep CRLF line endings. cmd.exe mis-parses LF-only
REM .bat files (drops leading characters, e.g. "setlocal" -> "tlocal").
REM ===========================================================================

for %%I in ("%~dp0..") do set "ROOT=%%~fI"
set "PORT=8010"
set "HEALTH=http://localhost:%PORT%/health"
set "WAIT_TRIES=60"

echo ============================================
echo   Production stack + public tunnel
echo   Project : %ROOT%
echo   Target  : %HEALTH%
echo ============================================
echo.

call :is_up
if not errorlevel 1 goto :app_ready

echo [*] App is not answering on port %PORT% yet.
where docker >nul 2>&1
if errorlevel 1 (
    echo [X] docker was not found on PATH.
    echo     Start Docker Desktop, wait for it to finish booting, then re-run.
    pause
    exit /b 1
)

echo [*] Starting containers ^(db + app^) ...
pushd "%ROOT%"
docker compose --env-file .env.production up -d db app
set "RC=%errorlevel%"
popd
if not "%RC%"=="0" (
    echo [X] docker compose failed with exit code %RC%.
    echo     Scroll up for the reason, or run it by hand:
    echo       cd /d "%ROOT%"
    echo       docker compose --env-file .env.production up -d db app
    pause
    exit /b 1
)

echo [*] Waiting for the app to become healthy ^(up to 180s^) ...
for /l %%N in (1,1,%WAIT_TRIES%) do (
    call :is_up
    if not errorlevel 1 goto :app_ready
    <nul set /p "=."
    timeout /t 3 /nobreak >nul
)
echo.
echo [X] App still not reachable at %HEALTH% after 180s.
echo     Check the container logs:
echo       docker compose logs --tail=80 app
pause
exit /b 1

:app_ready
echo.
echo [OK] App is up at %HEALTH%
echo.
call "%~dp0start-tunnel.bat" %PORT%
exit /b %errorlevel%

REM --- helpers --------------------------------------------------------------
REM errorlevel 0 = app answered 200 on /health, 1 = not reachable yet.
:is_up
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri '%HEALTH%' -UseBasicParsing -TimeoutSec 5; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }"
exit /b %errorlevel%
