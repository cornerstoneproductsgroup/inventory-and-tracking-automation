@echo off
setlocal
cd /d "%~dp0"

echo Running Lowe's Needs Invoicing automation...
python "lowes_tracking_automation.py" --config "config.example.json" --workflow invoice --submit
if errorlevel 1 (
  echo.
  echo Invoicing step failed.
  pause
  exit /b 1
)

echo.
echo Lowe's Invoicing run complete.
pause
