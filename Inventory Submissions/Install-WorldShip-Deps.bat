@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pip.exe" (
  echo No .venv found. Run Rebuild-Venv.bat first.
  pause
  exit /b 1
)

echo Installing WorldShip dependency ^(pywinauto^)...
".venv\Scripts\python.exe" -m pip install "pywinauto>=0.6.8"
if errorlevel 1 (
  echo pip install failed.
  pause
  exit /b 1
)

echo Done.
pause
exit /b 0
