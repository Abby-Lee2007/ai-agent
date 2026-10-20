@echo off
chcp 65001 >nul
cd /d "%~dp0"
python run_api.py --host 0.0.0.0 --port 8000
echo.
echo --- server stopped, press any key to exit ---
pause >nul
