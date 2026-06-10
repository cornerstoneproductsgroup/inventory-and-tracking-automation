@echo off
setlocal EnableExtensions
cd /d "%~dp0Inventory Submissions"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo WARN: .venv not found — using system python.
  set "PY=python"
)

echo.
echo === UPS.com Batch Shipping — Home Depot ===
echo.

"%PY%" -u run_ups_online_batch.py %*
set "EXITCODE=%ERRORLEVEL%"
echo.
if "%EXITCODE%"=="0" (
  echo Completed OK.
) else (
  echo Failed ^(exit %EXITCODE%^).
)
pause
exit /b %EXITCODE%
