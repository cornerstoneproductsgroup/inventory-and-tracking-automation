@echo off
setlocal
cd /d "%~dp0"

echo Running Lowe's Ship To Store automation...
python "lowes_tracking_automation.py" --config "config.example.json" --workflow ship_to_store --submit

if errorlevel 1 (
  echo.
  echo Script exited with an error.
) else (
  echo.
  echo Script completed.
)

pause
