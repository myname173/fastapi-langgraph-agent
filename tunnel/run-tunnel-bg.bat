@echo off
REM Background tunnel runner — launched detached (e.g. via Start-Process) so it
REM survives the parent shell. Writes all output to live.log so the URL can be
REM read. Auto-protocol (omit --protocol) lets cloudflared pick QUIC or HTTP/2
REM based on precheck — more resilient than forcing either on this fluctuating
REM network. --edge-ip-version 4 still forces IPv4 (IPv6 is unreliable here).
cd /d "%~dp0"
cloudflared.exe tunnel --no-autoupdate --edge-ip-version 4 --url http://localhost:8000 > live.log 2>&1
