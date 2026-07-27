@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv. Run setup.bat first.
  exit /b 1
)

set "UI_ONLY=1"
".venv\Scripts\python.exe" ui_demo.py
endlocal
