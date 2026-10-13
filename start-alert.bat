@echo off
cd /d "%~dp0"
if exist "%~dp0dist\PerplexityDoorBell.exe" (
  start "" "%~dp0dist\PerplexityDoorBell.exe"
  exit /b 0
)
set "PY=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PY%" set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo .venv not found
  pause
  exit /b 1
)
start "" "%PY%" "%~dp0app.py"
