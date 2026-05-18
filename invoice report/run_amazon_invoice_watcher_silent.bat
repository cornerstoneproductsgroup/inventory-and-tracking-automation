@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "LOG=%~dp0amazon_watcher.log"
set "PYTHONUNBUFFERED=1"
set "PYTHONIOENCODING=utf-8"
set "PY="

REM Same order as Run Full Workflow.bat — Inventory venv is the maintained environment.
if exist "%~dp0..\Inventory Submissions\.venv\Scripts\python.exe" (
  set "PY=%~dp0..\Inventory Submissions\.venv\Scripts\python.exe"
)
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"

echo.>>"%LOG%"
echo ===== %DATE% %TIME% watcher start =====>>"%LOG%"

if not defined PY (
  echo ERROR: No project venv found.>>"%LOG%"
  echo   Run: Inventory Submissions\Install-Deps.bat>>"%LOG%"
  echo   Or:  invoice report\install_deps.bat>>"%LOG%"
  echo ===== exit 1 %DATE% %TIME% =====>>"%LOG%"
  exit /b 1
)

echo Using: %PY%>>"%LOG%"
"%PY%" -c "import dotenv, pandas" >>"%LOG%" 2>&1
if errorlevel 1 (
  echo ERROR: This Python is missing packages ^(need dotenv, pandas^).>>"%LOG%"
  echo   Run: Inventory Submissions\Install-Deps.bat>>"%LOG%"
  echo   Or:  invoice report\install_deps.bat>>"%LOG%"
  echo ===== exit 1 %DATE% %TIME% =====>>"%LOG%"
  exit /b 1
)

"%PY%" "%~dp0amazon_invoice_watcher.py" >>"%LOG%" 2>&1
set "ERR=%ERRORLEVEL%"
echo ===== exit %ERR% %DATE% %TIME% =====>>"%LOG%"
exit /b %ERR%
