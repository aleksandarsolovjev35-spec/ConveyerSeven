@echo off
setlocal
cd /d "%~dp0\.."

if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv. Run setup.bat first.
  exit /b 1
)

".venv\Scripts\python.exe" -m compileall -q . || exit /b 1
".venv\Scripts\python.exe" -m unittest discover -s tests -v || exit /b 1

where node >nul 2>&1
if not errorlevel 1 (
  for %%F in (vision\ui\static\js\*.js) do node --check "%%F" || exit /b 1
  node --check tests\ui_interaction_matrix.js || exit /b 1
  if exist "node_modules\jsdom" (
    node tests\ui_interaction_matrix.js || exit /b 1
  ) else (
    echo JSDOM matrix skipped: run npm ci
  )
)

echo AUTOMATED CODE AND UI TESTS PASSED
endlocal
