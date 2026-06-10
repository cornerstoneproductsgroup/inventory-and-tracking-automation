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

"%PY%" -u test_worldship_ribbon_click.py > "%LOG%" 2>&1
set "EXITCODE=%ERRORLEVEL%"

type "%LOG%"
echo.
if "%EXITCODE%"=="0" (
  echo Test finished OK ^(exit 0^).
) else (
  echo Test FAILED ^(exit %EXITCODE%^). See log above: %LOG%
)
echo.
pause
exit /b %EXITCODE%
