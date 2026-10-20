@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "LAN_IP="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "$ip=(Get-NetIPConfiguration | Where-Object {$_.IPv4DefaultGateway -ne $null} | Select-Object -First 1 -ExpandProperty IPv4Address).IPAddress; if($ip){$ip}"`) do set "LAN_IP=%%i"
echo Starting AI Banking Agent H5 on all network interfaces...
start "AI Banking Agent API" /b python run_api.py --host 0.0.0.0 --port 8000
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:8000"
echo.
echo This computer: http://127.0.0.1:8000
if defined LAN_IP echo Same Wi-Fi phone: http://%LAN_IP%:8000
echo If another device cannot open the LAN URL, allow TCP port 8000 in Windows Firewall.
echo Close this window when the demo is finished.
pause >nul
