@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM Broken installs (e.g. partial C:\Python314) confuse venvs. Clear inherited vars.
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONNOUSERSITE=1"

echo.
echo This folder: %CD%
echo.
echo Deletes .venv here, then creates a new venv with a working Python, pip installs,
echo and runs Playwright Chromium install.
echo.
echo Optional: set FORCE_PYTHON to a known-good python.exe before running this bat, e.g.:
echo   set FORCE_PYTHON=%LOCALAPPDATA%\Programs\Python\Python313\python.exe
echo   Rebuild-Venv.bat
echo.
pause

if exist ".venv" (
  echo Removing old .venv ...
  rmdir /s /q ".venv"
)

set "CREATED="
set "PYEXE="

if defined FORCE_PYTHON (
  if exist "%FORCE_PYTHON%" (
    set "PYEXE=%FORCE_PYTHON%"
    echo Using FORCE_PYTHON: %PYEXE%
    goto CREATE
  ) else (
    echo ERROR: FORCE_PYTHON is set but file not found: %FORCE_PYTHON%
    pause
    exit /b 1
  )
)

REM Prefer python.org "per user" installs (usually complete; avoids broken C:\PythonXX).
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
  set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  goto CREATE
)
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
  set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  goto CREATE
)
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
  set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  goto CREATE
)

echo No Python under %%LOCALAPPDATA%%\Programs\Python\ — trying py launcher (specific versions first)...
py -3.12 -m venv .venv
if exist ".venv\Scripts\python.exe" set "CREATED=1"
if defined CREATED goto VERIFY

py -3.13 -m venv .venv
if exist ".venv\Scripts\python.exe" set "CREATED=1"
if defined CREATED goto VERIFY

py -3.11 -m venv .venv
if exist ".venv\Scripts\python.exe" set "CREATED=1"
if defined CREATED goto VERIFY

py -3 -m venv .venv
if exist ".venv\Scripts\python.exe" set "CREATED=1"
if defined CREATED goto VERIFY

python -m venv .venv
if exist ".venv\Scripts\python.exe" set "CREATED=1"
if defined CREATED goto VERIFY

echo ERROR: Could not create .venv. Install Python 3.11+ from https://www.python.org/downloads/
echo (enable "Add python.exe to PATH"), then run again — or set FORCE_PYTHON to a full path
echo to a working python.exe (not a broken C:\Python314 tree).
pause
exit /b 1

:CREATE
echo Creating venv with: %PYEXE%
"%PYEXE%" -m venv .venv
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: venv creation failed.
  pause
  exit /b 1
)

:VERIFY
echo Verifying venv can import the standard library...
".venv\Scripts\python.exe" -c "import encodings, sys; print(sys.version)"
if errorlevel 1 (
  echo.
  echo ERROR: This venv's base Python is broken or incomplete (common with C:\Python314).
  echo Remove .venv, install Python from python.org, then either:
  echo   set FORCE_PYTHON=full\path\to\python.exe
  echo   Rebuild-Venv.bat
  echo or ensure Python313/Python312 exists under:
  echo   %LOCALAPPDATA%\Programs\Python\
  rmdir /s /q ".venv" 2>nul
  pause
  exit /b 1
)

echo.
echo Upgrading pip...
".venv\Scripts\python.exe" -m pip install --upgrade pip

echo.
echo Installing requirements...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo pip install failed.
  pause
  exit /b 1
)

echo.
echo Installing Playwright Chromium...
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 (
  echo playwright install failed.
  pause
  exit /b 1
)

echo.
echo Done. If Run Full Workflow still fails, remove any bad Python from PATH or avoid C:\Python314.
echo.
pause
exit /b 0
