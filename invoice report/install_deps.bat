@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
set "PIP=%~dp0.venv\Scripts\pip.exe"

if not exist "%PY%" (
  echo Creating invoice report\.venv ...
  py -3.13 -m venv .venv
  if not exist "%PY%" (
    python -m venv .venv
  )
)
if not exist "%PY%" (
  echo ERROR: Could not create .venv here.
  pause
  exit /b 1
)

echo Installing into %PY%
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo pip install failed.
  pause
  exit /b 1
)

echo Installing Playwright Chromium ...
"%PY%" -m playwright install chromium

echo.
echo Done. Amazon watcher can use this venv or Inventory Submissions\.venv.
pause
exit /b 0
