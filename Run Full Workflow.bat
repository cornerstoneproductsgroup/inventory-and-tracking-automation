@echo off
setlocal
cd /d "%~dp0"

set "INV_PY=%~dp0Inventory Submissions\.venv\Scripts\python.exe"
set "RUNNER=python"
if exist "%INV_PY%" set "RUNNER=%INV_PY%"

echo Running full workflow: Rithum inventory -^> Depot tracking/invoicing -^> Lowe's -^> SPS Tractor Supply...
if /I not "%RUNNER%"=="python" echo Using: %RUNNER%
echo.

"%RUNNER%" "run_full_workflow.py" %*
set "ERR=%ERRORLEVEL%"

echo.
if not "%ERR%"=="0" (
  echo One or more steps failed ^(exit %ERR%^).
) else (
  echo All steps finished successfully.
)

pause
exit /b %ERR%
