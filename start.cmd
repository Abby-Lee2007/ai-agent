@echo off
chcp 65001 >nul
rem ===========================================================================
rem  AI Banking Agent - double click this file to start chatting.
rem  No dependency installation required (standard library only).
rem
rem  You can also pass arguments, for example:
rem      start.cmd --today 2026-11-08
rem      start.cmd --llm on
rem      start.cmd --script scripts\demo_all.txt
rem      start.cmd --help
rem ===========================================================================
cd /d "%~dp0"
python run.py %*
echo.
echo --- session closed, press any key to exit ---
pause >nul
