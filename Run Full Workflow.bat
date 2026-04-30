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
echo   1  All steps ^(default^)
echo   2  Skip CommerceHub ^(SPS inventory + SPS tracking^)
echo   3  Skip SPS inventory ^(CommerceHub + SPS tracking^)
echo   4  SPS tracking only ^(skip CommerceHub + SPS inventory — already in SPS^)
echo   5  Skip SPS tracking ^(CommerceHub + SPS inventory only^)
echo   6  Tracking + invoicing only ^(skip CH inventory + SPS inv; Depot/Lowe's + SPS tracking^)
echo   7  Cancel
echo.
choice /C 1234567 /N /M "Press 1-7: "
if errorlevel 7 exit /b 0
if errorlevel 6 (
  set "EXTRA_ARGS=--tracking-invoicing-only"
  goto RUN
)
if errorlevel 5 (
  set "EXTRA_ARGS=--skip-sps-tracking"
  goto RUN
)
if errorlevel 4 (
  set "EXTRA_ARGS=--skip-commercehub --skip-sps-inventory"
  goto RUN
)
if errorlevel 3 (
  set "EXTRA_ARGS=--skip-sps-inventory"
  goto RUN
)
if errorlevel 2 (
  set "EXTRA_ARGS=--skip-commercehub"
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
