@echo off
setlocal
cd /d "%~dp0"
call preflight_checks\07_AUTOMATED_CODE_AND_UI_TESTS.bat
exit /b %ERRORLEVEL%
