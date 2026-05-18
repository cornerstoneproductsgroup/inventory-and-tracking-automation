@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Amazon Invoice Watcher

set "PY="
if exist "%~dp0..\Inventory Submissions\.venv\Scripts\python.exe" (
  set "PY=%~dp0..\Inventory Submissions\.venv\Scripts\python.exe"
)
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"

if not defined PY (
  echo ERROR: No project venv found.
  echo   Run: Inventory Submissions\Install-Deps.bat
  echo   Or:  invoice report\install_deps.bat
  pause
  exit /b 1
)

"%PY%" -c "import dotenv, pandas" 2>nul
if errorlevel 1 (
  echo ERROR: Python at %PY% is missing packages.
  echo   Run: Inventory Submissions\Install-Deps.bat
  echo   Or:  invoice report\install_deps.bat
  pause
  exit /b 1
)

echo.
echo Amazon Invoice Watcher — keeps running until you close this window.
echo Using: %PY%
echo Input:  \\rygarcorp.com\shares\Cornerstone\Invoice Reports\Amazon\Input
echo Output: \\rygarcorp.com\shares\Cornerstone\Invoice Reports\Amazon\Output
echo   ^(override AMAZON_INVOICE_INPUT_DIR / AMAZON_INVOICE_OUTPUT_DIR in invoice report\.env^)
echo.
echo When you save a new raw export there, it will be formatted and printed.
echo Log: %~dp0amazon_watcher.log
echo.

"%PY%" "%~dp0amazon_invoice_watcher.py" %*
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" pause
exit /b %ERR%
