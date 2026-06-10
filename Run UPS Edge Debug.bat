@echo off
setlocal EnableExtensions

rem WARNING: Huntress and other EDR tools flag this as credential-theft behavior.
rem Use only if IT approved UPS_ALLOW_UNSAFE_CDP=1. Prefer --setup-login instead.

rem Start Edge with remote debugging so UPS automation can attach.
rem Leave this window open while the batch job runs, or start before Run UPS Online Batch.

set "UPS_BROWSER_CDP_PORT=9345"
set "UPS_HOME_URL=https://www.ups.com/us/en/home"

for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('LocalApplicationData')"`) do set "LOCALAPPDATA=%%D"

set "EDGE=%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"
set "USER_DATA=%LOCALAPPDATA%\Microsoft\Edge\User Data"

if not exist "%EDGE%" (
  set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
)
if not exist "%EDGE%" (
  set "EDGE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
)
if not exist "%EDGE%" (
  echo ERROR: Microsoft Edge not found.
  pause
  exit /b 1
)

echo Closing existing Edge...
taskkill /F /IM msedge.exe /T >nul 2>&1
timeout /t 2 /nobreak >nul

echo Starting Edge with debug port %UPS_BROWSER_CDP_PORT% and opening UPS...
start "" "%EDGE%" --user-data-dir="%USER_DATA%" --profile-directory=Default --remote-debugging-port=%UPS_BROWSER_CDP_PORT% --remote-debugging-address=127.0.0.1 --remote-allow-origins=* --no-first-run --disable-restore-session-state --new-window "%UPS_HOME_URL%"

echo.
echo Edge started. Run UPS Online Batch, or set UPS_BROWSER_MODE=manual in .env.
echo Debug port: %UPS_BROWSER_CDP_PORT%
pause
