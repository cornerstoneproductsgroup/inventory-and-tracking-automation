@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo WARN: .venv not found — using system python.
  set "PY=python"
)

if not exist "logs" mkdir "logs"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%i"
set "LOG=logs\worldship_ribbon_test_%STAMP%.log"

echo.
echo === WorldShip ribbon click test ===
echo Log file: %LOG%
echo.
echo Prerequisites:
echo   - Run this ON the WorldShip PC ^(not a remote session unless WorldShip is there^).
echo   - Open UPS WorldShip first ^(or pin it to the taskbar so auto-start can find it^).
echo.
echo Progress appears below in real time ^(also saved to the log^).
echo If nothing prints for 30+ seconds, WorldShip may still be loading — wait up to 2 min.
echo.

rem Shorter retries for a calibration run (override in .env if needed)
if not defined WORLDSHIP_BATCH_IMPORT_ATTEMPTS set "WORLDSHIP_BATCH_IMPORT_ATTEMPTS=3"
if not defined WORLDSHIP_BATCH_IMPORT_VERIFY_S set "WORLDSHIP_BATCH_IMPORT_VERIFY_S=1.5"

"%PY%" -u test_worldship_ribbon_click.py 2>&1 | powershell -NoProfile -Command "$log='%LOG%'; $input | ForEach-Object { $_; Add-Content -LiteralPath $log -Value $_ }"
set "EXITCODE=%ERRORLEVEL%"

echo.
if "%EXITCODE%"=="0" (
  echo Test finished OK ^(exit 0^). Full log: %LOG%
) else (
  echo Test FAILED ^(exit %EXITCODE%^). Full log: %LOG%
)
echo.
pause
exit /b %EXITCODE%
