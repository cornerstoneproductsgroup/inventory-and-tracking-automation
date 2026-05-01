@echo off
setlocal
cd /d "%~dp0"

set "INV_PY=%~dp0Inventory Submissions\.venv\Scripts\python.exe"
set "RUNNER=python"
if exist "%INV_PY%" set "RUNNER=%INV_PY%"

set "EXTRA_ARGS="
if not "%~1"=="" goto RUN

echo.
echo Which steps to run? ^(keyboard — no Enter needed^)
echo   1  All Steps
echo   2  Depot Tracking and Invoicing
echo   3  Lowe's Tracking and Invoicing
echo   4  Commercehub and SPS Inventory + Depot Tracking Invoicing
echo   5  Commercehub and SPS Inventory + Lowe's Tracking Invoicing
echo   6  Commercehub and SPS Inventory + Depot and Lowe's Tracking Invoicing
echo   7  SPS Full ^(SPS Inventory and Tracking^)
echo   8  Commercehub Inventory
echo   9  SPS Inventory
echo   A  All Tracking and Invoicing ^(No Inventory^)
echo.
choice /C 123456789A /N /M "Press 1-9 or A: "
if errorlevel 10 (
  set "EXTRA_ARGS=--tracking-invoicing-only"
  goto RUN
)
if errorlevel 9 (
  set "EXTRA_ARGS=--skip-commercehub --skip-sps-tracking"
  goto RUN
)
if errorlevel 8 (
  set "EXTRA_ARGS=--skip-sps-inventory --skip-sps-tracking --skip-depot --skip-lowes"
  goto RUN
)
if errorlevel 7 (
  set "EXTRA_ARGS=--skip-commercehub"
  goto RUN
)
if errorlevel 6 (
  set "EXTRA_ARGS=--skip-sps-tracking"
  goto RUN
)
if errorlevel 5 (
  set "EXTRA_ARGS=--skip-sps-tracking --skip-depot"
  goto RUN
)
if errorlevel 4 (
  set "EXTRA_ARGS=--skip-sps-tracking --skip-lowes"
  goto RUN
)
if errorlevel 3 (
  set "EXTRA_ARGS=--skip-sps-inventory --skip-sps-tracking --skip-inventory --skip-depot"
  goto RUN
)
if errorlevel 2 (
  set "EXTRA_ARGS=--skip-sps-inventory --skip-sps-tracking --skip-inventory --skip-lowes"
  goto RUN
)

:RUN
echo.
echo Running workflow...
if /I not "%RUNNER%"=="python" echo Using: %RUNNER%
if defined EXTRA_ARGS echo Options: %EXTRA_ARGS%
echo.

"%RUNNER%" "run_full_workflow.py" %EXTRA_ARGS% %*
set "ERR=%ERRORLEVEL%"

echo.
if not "%ERR%"=="0" (
  echo One or more steps failed ^(exit %ERR%^).
) else (
  echo All steps finished successfully.
)

pause
exit /b %ERR%
