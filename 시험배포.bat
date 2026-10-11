@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
echo ==== 시험배포: 우리 마지 PC 에만 먼저 보냅니다 ====
python tools\release.py beta
pause
