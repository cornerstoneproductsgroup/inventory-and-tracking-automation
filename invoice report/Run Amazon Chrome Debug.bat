@echo off
setlocal
cd /d "%~dp0"

set "PORT=9348"
if defined AMAZON_CHROME_CDP_PORT set "PORT=%AMAZON_CHROME_CDP_PORT%"

set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (
  echo Chrome not found.
  pause
  exit /b 1
)

echo Starting Chrome with remote debugging on port %PORT%...
echo Uses your normal profile — stay logged in to Seller Central.
echo.
echo Keep this Chrome open, then run Amazon Seller Download.
echo Or set in .env: AMAZON_CHROME_CDP_URL=http://127.0.0.1:%PORT%
echo.

start "" "%CHROME%" --remote-debugging-port=%PORT% --remote-debugging-address=127.0.0.1 --remote-allow-origins=* "https://sellercentral.amazon.com/home"

exit /b 0
