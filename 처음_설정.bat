@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
where git >nul 2>&1
if errorlevel 1 goto nogit
where python >nul 2>&1
if errorlevel 1 goto nopy
if exist ".git" goto already
echo.
echo ==== 처음 설정: 이 폴더를 GitHub 저장소 bak-watch 와 연결합니다 ====
echo 먼저 github.com 에서 bak-watch 라는 이름의 Public 저장소를 빈 상태로 만들어 두세요.
echo.
set /p GHID=GitHub 아이디: 
set /p GHMAIL=GitHub 가입 이메일: 
git init -b main
git config user.name "%GHID%"
git config user.email "%GHMAIL%"
git remote add origin https://github.com/%GHID%/bak-watch.git
python tools\release.py check
if errorlevel 1 goto fail
git add -A
git commit -m "first upload"
echo.
echo GitHub 로그인 창이 뜨면 로그인하세요.
git push -u origin main
if errorlevel 1 goto fail
> 업데이트주소.txt echo [시험판 주소 - 우리 마지 PC config.ini 의 update_url 에]
>> 업데이트주소.txt echo https://raw.githubusercontent.com/%GHID%/bak-watch/main/release/beta/manifest.json
>> 업데이트주소.txt echo.
>> 업데이트주소.txt echo [전체 주소 - 다른 업체 PC config.ini 의 update_url 에]
>> 업데이트주소.txt echo https://raw.githubusercontent.com/%GHID%/bak-watch/main/release/stable/manifest.json
echo.
echo 연결 완료. 업데이트 주소를 업데이트주소.txt 에 저장했습니다.
type 업데이트주소.txt
echo.
echo 다음: 시험배포.bat 을 실행하세요.
pause
exit /b
:nogit
echo [필요] Git 이 설치되어 있지 않습니다.
echo 시작 메뉴에서 PowerShell 을 열고 다음 줄을 붙여넣으세요:  winget install --id Git.Git -e
echo 설치 후 이 창을 닫고 다시 실행하세요.
pause
exit /b
:nopy
echo [필요] Python 이 설치되어 있지 않습니다. python.org 에서 Python 3.13 설치 - 첫 화면 Add python.exe to PATH 체크
pause
exit /b
:already
echo 이미 GitHub 와 연결되어 있습니다. 시험배포.bat 을 쓰세요.
pause
exit /b
:fail
echo.
echo [실패] 위 메시지를 캡처해서 Claude 에게 보여주세요.
pause
