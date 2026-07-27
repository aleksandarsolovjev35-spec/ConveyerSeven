@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  py -3.11 -m venv .venv || exit /b 1
)

".venv\Scripts\python.exe" -m pip install --upgrade pip || exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements-ui.txt || exit /b 1

echo UI-only setup complete.
endlocal
