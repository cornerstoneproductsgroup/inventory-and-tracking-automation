@echo off
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0..\Inventory Submissions\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo Amazon invoice: format newest raw file once (then exit).
echo   Continuous watcher: Run Amazon Invoice Watcher.bat  (repo root)
echo.

"%PY%" "%~dp0amazon_invoice_postprocess.py" %*
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" pause
exit /b %ERR%
