@echo off
setlocal EnableExtensions

rem Start Chrome with remote debugging so UPS automation can attach.
rem Leave this window open while the batch job runs, or start before Run UPS Online Batch.

set "UPS_CHROME_CDP_PORT=9344"
set "UPS_HOME_URL=https://www.ups.com/us/en/home"

for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('LocalApplicationData')"`) do set "LOCALAPPDATA=%%D"

set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
set "USER_DATA=%LOCALAPPDATA%\Google\Chrome\User Data"

if not exist "%CHROME%" (
  echo ERROR: Chrome not found at %CHROME%
  pause
  exit /b 1
)

echo Closing existing Chrome...
taskkill /F /IM chrome.exe /T >nul 2>&1
timeout /t 2 /nobreak >nul

echo Starting Chrome with debug port %UPS_CHROME_CDP_PORT% and opening UPS...
start "" "%CHROME%" --user-data-dir="%USER_DATA%" --profile-directory=Default --remote-debugging-port=%UPS_CHROME_CDP_PORT% --remote-debugging-address=127.0.0.1 --remote-allow-origins=* --no-first-run --disable-restore-session-state --new-window "%UPS_HOME_URL%"

echo.
echo Chrome started. Run UPS Online Batch, or set UPS_BROWSER_MODE=manual in .env.
echo Debug port: %UPS_CHROME_CDP_PORT%
pause
