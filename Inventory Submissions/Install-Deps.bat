@echo off
setlocal
cd /d "%~dp0"

set "PYTHONHOME="
set "PYTHONPATH="

if not exist ".venv\Scripts\python.exe" (
  echo No .venv here. Run Rebuild-Venv.bat first.
  pause
  exit /b 1
)

echo Installing packages from requirements.txt ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo pip install failed.
  pause
  exit /b 1
)

echo.
echo Installing Playwright Chromium ...
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 (
  echo playwright install failed.
  pause
  exit /b 1
)

echo.
echo Done.
pause
exit /b 0
