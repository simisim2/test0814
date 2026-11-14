@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [오류] Python 이 설치되어 있지 않습니다. https://www.python.org/downloads/ 에서 설치하세요.
  echo        설치 화면에서 "Add python.exe to PATH" 를 꼭 체크하세요.
  pause
  exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo [안내] ffmpeg 가 없습니다. 영상 길이 표시·자막·렌더에 필요합니다.
  echo        설치:  winget install Gyan.FFmpeg   (설치 후 이 창을 닫고 다시 실행)
  echo.
)

if not exist .venv (
  echo 처음 한 번만 준비 중입니다...
  python -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt

python run.py
pause
