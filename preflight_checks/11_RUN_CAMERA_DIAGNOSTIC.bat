@echo off
setlocal
cd /d "%~dp0\.."

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Missing .venv. Run setup.bat first.
  exit /b 1
)

"%PY%" preflight_checks\11_camera_open_diagnostic.py
exit /b %ERRORLEVEL%
