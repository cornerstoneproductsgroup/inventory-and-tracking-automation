@echo off
setlocal
cd /d "%~dp0"

echo Running Lowe's Tracking + Invoicing (one session)...
echo Order: Ship To Store -^> Ship To Customer -^> Needs Invoicing
python "lowes_tracking_automation.py" --config "config.example.json" --workflow all --submit
if errorlevel 1 (
  echo.
  echo Run failed.
  pause
  exit /b 1
)

echo.
echo Lowe's Tracking + Invoicing run complete.
pause
