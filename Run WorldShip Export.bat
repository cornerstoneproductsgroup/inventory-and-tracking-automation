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
set "LOG=logs\worldship_export_%STAMP%.log"

echo.
echo === UPS WorldShip Batch Export ^(Depot Shipments tracking CSV^) ===
echo Log: %LOG%
echo.

"%PY%" -u run_worldship_export.py %* 2>&1 | powershell -NoProfile -Command "$log='%LOG%'; $input | ForEach-Object { $_; Add-Content -LiteralPath $log -Value $_ }"
set "EXITCODE=%ERRORLEVEL%"
echo.
if "%EXITCODE%"=="0" (
  echo Completed OK. Log: %LOG%
) else (
  echo Failed ^(exit %EXITCODE%^). See log: %LOG%
)
pause
exit /b %EXITCODE%
