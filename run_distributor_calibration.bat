@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv. Run setup.bat first.
  exit /b 1
)

echo DISTRIBUTOR CALIBRATION
echo Real DIST1/DIST2 motion and G28 homing will be used.
echo Keep the physical E-stop within immediate reach.
echo.
".venv\Scripts\python.exe" calibrate_distributor.py --port COM4
endlocal
