@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install --upgrade requests beautifulsoup4 playwright keyring psutil
python -m playwright install chromium
if not exist config.ini copy config.example.ini config.ini
echo.
echo Done.
pause
