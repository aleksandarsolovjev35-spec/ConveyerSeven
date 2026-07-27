@echo off
setlocal
cd /d "%~dp0"

py -3.11 -m venv .venv || exit /b 1
".venv\Scripts\python.exe" -m pip install --upgrade pip || exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements.txt || exit /b 1

where npm >nul 2>&1
if not errorlevel 1 (
  npm ci --ignore-scripts --no-audit --no-fund || exit /b 1
)

echo Setup complete.
endlocal
