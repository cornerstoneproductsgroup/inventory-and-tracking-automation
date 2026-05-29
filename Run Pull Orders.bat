@echo off
setlocal
cd /d "%~dp0Inventory Submissions"

set "INV_PY=%~dp0Inventory Submissions\.venv\Scripts\python.exe"
set "RUNNER=python"
if exist "%INV_PY%" set "RUNNER=%INV_PY%"

echo.
echo Pull Orders — CommerceHub PDF/CSV, SPS Tractor/Grainger, warehouse print
echo.
"%RUNNER%" "run_pull_orders.py" %*
set "ERR=%ERRORLEVEL%"

echo.
if not "%ERR%"=="0" (
  echo Pull orders finished with errors ^(exit %ERR%^).
) else (
  echo Pull orders finished successfully.
)
pause
exit /b %ERR%
