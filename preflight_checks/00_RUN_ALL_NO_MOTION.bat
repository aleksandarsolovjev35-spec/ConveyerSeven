@echo off
setlocal
cd /d "%~dp0\.."

call preflight_checks\00_RUN_SOFTWARE_ONLY.bat || exit /b 1

set "PY=.venv\Scripts\python.exe"
"%PY%" preflight_checks\05_seven_cameras_check.py || exit /b 1
"%PY%" preflight_checks\06_controller_no_motion_check.py || exit /b 1

echo.
echo FULL NO-MOTION PREFLIGHT PASSED
endlocal
