@echo off
chcp 65001 >nul
set "SRC=%~dp0"
set "DST=C:\bak_watch"
if not exist "%DST%\config.ini" (
  echo [중단] C:\bak_watch 에 기존 프로그램이 없습니다. 처음 설치라면 설치안내.txt 를 보세요.
  pause
  exit /b
)
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmm"') do set "TS=%%i"
echo.
echo 1) 실행 중인 알림 프로그램(검은 창)을 멈춥니다...
taskkill /FI "WINDOWTITLE eq BAK-WATCH*" /T /F >nul 2>&1
timeout /t 3 >nul
echo 2) 기존 폴더 전체 백업 → C:\bak_watch_백업_%TS%
robocopy "%DST%" "C:\bak_watch_백업_%TS%" /E /NFL /NDL /NJH /NJS >nul
if not exist "C:\bak_watch_백업_%TS%\bak_watch.py" (
  echo [중단] 백업이 만들어지지 않았습니다. 아무것도 바꾸지 않았습니다.
  pause
  exit /b
)
echo 3) 새 파일 복사 (config.ini, state.json 은 그대로 둠)
for %%f in (bak_watch.py watchdog.py set_password.py 1_install.bat 2_test.bat 3_run.bat 비밀번호_변경.bat config.example.ini 설치안내.txt 멈춤수정_안내.txt) do copy /Y "%SRC%%%f" "%DST%\" >nul
echo 4) 필요한 부품 설치 (몇 분 걸릴 수 있음)
cd /d "%DST%"
python -m pip install --upgrade requests beautifulsoup4 playwright keyring psutil
echo.
echo ========================================================
echo  완료. 백업 위치: C:\bak_watch_백업_%TS%
echo  다음 순서:  C:\bak_watch\비밀번호_변경.bat  →  C:\bak_watch\3_run.bat
echo  되돌리려면: 검은 창을 닫고 백업 폴더 내용을 C:\bak_watch 에 다시 복사
echo ========================================================
pause
