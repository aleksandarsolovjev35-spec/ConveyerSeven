@echo off
setlocal
cd /d "%~dp0"

py -3.11 -m venv .venv || exit /b 1
".venv\Scripts\python.exe" -m pip install --upgrade pip || exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements.txt || exit /b 1


echo Setup complete.
endlocal
