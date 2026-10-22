@echo off
chcp 65001 >nul
cd /d "%~dp0"
netstat -ano | findstr /R /C:":8000 .*LISTENING" >nul
if not errorlevel 1 goto already_running
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.9+ or add it to PATH.
  pause >nul
  exit /b 1
)
set "LAN_IP="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "$ip=(Get-NetIPConfiguration | Where-Object {$_.IPv4DefaultGateway -ne $null} | Select-Object -First 1 -ExpandProperty IPv4Address).IPAddress; if($ip){$ip}"`) do set "LAN_IP=%%i"
echo Starting AI Banking Agent H5 on all network interfaces...
start "AI Banking Agent API" /b python run_api.py --host 0.0.0.0 --port 8000
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:8000"
goto show_info

:already_running
echo AI Banking Agent is already running.
start "" "http://127.0.0.1:8000"

:show_info
echo.
echo This computer: http://127.0.0.1:8000
if defined LAN_IP echo Same Wi-Fi phone: http://%LAN_IP%:8000
echo If another device cannot open the LAN URL, allow TCP port 8000 in Windows Firewall.
echo Close this window when the demo is finished.
pause >nul
