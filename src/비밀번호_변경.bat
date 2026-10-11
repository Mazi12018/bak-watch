@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
python set_password.py
pause
