@echo off
setlocal
cd /d "%~dp0Inventory Submissions"

set "INV_PY=%~dp0Inventory Submissions\.venv\Scripts\python.exe"
set "RUNNER=python"
if exist "%INV_PY%" set "RUNNER=%INV_PY%"

echo.
echo WorldShip Batch Import — Import-Export tab through Import/Export Preview
echo.
"%RUNNER%" "run_worldship_import.py" %*
set "ERR=%ERRORLEVEL%"

echo.
if not "%ERR%"=="0" (
  echo WorldShip import step finished with errors ^(exit %ERR%^).
) else (
  echo WorldShip import step finished successfully.
)
pause
exit /b %ERR%
