@echo off
setlocal
cd /d "%~dp0"

echo Running Lowe's Ship To Customer automation (Dry Run)...
python "lowes_tracking_automation.py" --config "config.example.json" --workflow ship_to_customer

if errorlevel 1 (
  echo.
  echo Script exited with an error.
) else (
  echo.
  echo Script completed.
)

echo.
echo Brief pause so you can read this window...
timeout /t 12 /nobreak >nul

pause
