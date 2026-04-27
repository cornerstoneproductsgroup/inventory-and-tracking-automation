@echo off
setlocal
cd /d "%~dp0"

echo Running Lowe's Tracking + Invoicing (Dry Run, one session)...
python "lowes_tracking_automation.py" --config "config.example.json" --workflow all
if errorlevel 1 (
  echo.
  echo Run failed.
  pause
  exit /b 1
)

echo.
echo Dry run complete.
timeout /t 20 /nobreak >nul
pause
