@echo off
setlocal
cd /d "%~dp0"

set "INV_PY=%~dp0Inventory Submissions\.venv\Scripts\python.exe"
set "RUNNER=python"
if exist "%INV_PY%" set "RUNNER=%INV_PY%"

echo.
echo Scheduled workflow — runs steps from scheduled_workflow.json
echo   1. Pull Orders
echo   2. FedEx Batch
echo   3. All Invoice Reports
echo   4. All Inventories
echo.
echo Edit scheduled_workflow.json to add steps or change order.
echo Install daily 5:00 AM run: Install-Morning-Schedule-Task.bat
echo.

"%RUNNER%" "run_scheduled_workflow.py" %*
set "ERR=%ERRORLEVEL%"

echo.
if not "%ERR%"=="0" (
  echo One or more scheduled steps failed ^(exit %ERR%^).
  echo Log: logs\scheduled_workflow.log
) else (
  echo All scheduled steps finished successfully.
)

pause
exit /b %ERR%
