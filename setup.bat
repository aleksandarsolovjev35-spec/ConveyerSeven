@echo off
setlocal
cd /d "%~dp0"

rem Ищем доступную версию Python 3.11+ через py-лаунчер.
rem Код проекта использует только синтаксис 3.10+, поэтому подойдёт любая
rem из 3.11/3.12/3.13; сначала пробуем 3.11 (документированная версия).
py -3.11 -m venv .venv 2>nul || py -3.12 -m venv .venv 2>nul || py -3.13 -m venv .venv 2>nul || py -3 -m venv .venv || exit /b 1
".venv\Scripts\python.exe" -m pip install --upgrade pip || exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements.txt || exit /b 1


echo Setup complete.
endlocal
