@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
title BAK-WATCH (do not close)
:loop
python watchdog.py
if %errorlevel%==9 goto end
echo Restarting watchdog in 30 seconds...
timeout /t 30 >nul
goto loop
:end
echo Already running in another window. This window will close.
timeout /t 5 >nul
