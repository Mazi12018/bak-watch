@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
echo ==== 되돌리기: 모든 업체 PC 를 직전 전체배포 버전으로 되돌립니다 ====
set /p OK=진행하려면 y 입력 후 Enter: 
if /i not "%OK%"=="y" goto cancel
python tools\release.py rollback
pause
exit /b
:cancel
echo 취소했습니다.
pause
