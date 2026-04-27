@echo off
setlocal
cd /d "%~dp0"

echo Running Lowe's Ship To Store automation (Dry Run)...
python "lowes_tracking_automation.py" --config "config.example.json" --workflow ship_to_store

if errorlevel 1 (
  echo.
  echo Script exited with an error.
) else (
  echo.
  echo Script completed.
)

echo.
echo Extra pause so you can read this window (ship-to-store dry run)...
timeout /t 30 /nobreak >nul

pause
