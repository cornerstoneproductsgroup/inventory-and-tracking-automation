@echo off
setlocal
cd /d "%~dp0"
REM Avoid broken pip.exe launchers (stale Python314 AppData path). Use 3.13 explicitly.
py -3.13 -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
py -3.13 -m playwright install chromium
exit /b %ERRORLEVEL%
