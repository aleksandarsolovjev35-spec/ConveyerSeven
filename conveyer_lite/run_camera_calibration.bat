@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Python environment not found: .venv
  exit /b 1
)

".venv\Scripts\python.exe" -m vision.camera_calibration_console --config camera_mapping.json
exit /b %ERRORLEVEL%
