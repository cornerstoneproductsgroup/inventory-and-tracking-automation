@echo off
setlocal EnableExtensions
cd /d "%~dp0Inventory Submissions"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo WARN: .venv not found — using system python.
  set "PY=python"
)

if not exist "logs" mkdir "logs"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%i"
set "LOG=logs\ups_void_%STAMP%.log"

echo.
echo === UPS Void Shipments — today's Shipping History ===
echo Log: %LOG%
echo.

"%PY%" -u run_ups_void_shipments.py %* > "%LOG%" 2>&1
set "EXITCODE=%ERRORLEVEL%"
type "%LOG%"
echo.
if "%EXITCODE%"=="0" (
  echo Completed OK.
) else (
  echo Failed ^(exit %EXITCODE%^). See log: %LOG%
)
pause
exit /b %EXITCODE%
