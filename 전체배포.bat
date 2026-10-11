@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
echo ==== 전체배포: 시험배포한 버전을 모든 업체 PC 에 보냅니다 ====
echo 우리 마지 PC 에서 하루 이상 문제없이 돌았는지 확인했나요?
set /p OK=진행하려면 y 입력 후 Enter: 
if /i not "%OK%"=="y" goto cancel
python tools\release.py stable
pause
exit /b
:cancel
echo 취소했습니다.
pause
