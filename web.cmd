@echo off
chcp 65001 >nul
rem ===========================================================================
rem  AI Banking Agent - web demo (double click to start)
rem  Then open http://127.0.0.1:8000 on this PC,
rem  or http://<this-pc-lan-ip>:8000 on your phone (same Wi-Fi).
rem ===========================================================================
cd /d "%~dp0"
python web_app.py --host 0.0.0.0 --port 8000
echo.
echo --- server stopped, press any key to exit ---
pause >nul
