@echo off
setlocal
cd /d "%~dp0\.."

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Missing .venv. Run setup.bat first.
  exit /b 1
)

"%PY%" preflight_checks\01_environment_check.py || exit /b 1
"%PY%" preflight_checks\02_configuration_check.py || exit /b 1
"%PY%" preflight_checks\03_model_files_check.py || exit /b 1
"%PY%" preflight_checks\04_model_load_and_warmup.py || exit /b 1
call preflight_checks\07_AUTOMATED_CODE_AND_UI_TESTS.bat || exit /b 1

echo.
echo SOFTWARE-ONLY PREFLIGHT PASSED
endlocal
