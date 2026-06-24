@echo off
setlocal
cd /d "%~dp0"

echo.
echo BLOCKED: This script starts Chrome with remote debugging.
echo That pattern triggers security alerts (e.g. Huntress infostealer detection).
echo.
echo Amazon Seller Download uses Playwright direct control instead — no debug port.
echo Run "Run Amazon Seller Download.bat" normally.
echo.
echo Only if IT has explicitly approved remote debugging:
echo   set AMAZON_ALLOW_UNSAFE_CDP=1
echo   then run this batch again.
echo.

if not "%AMAZON_ALLOW_UNSAFE_CDP%"=="1" (
  pause
  exit /b 1
)

set "PORT=9348"
if defined AMAZON_CHROME_CDP_PORT set "PORT=%AMAZON_CHROME_CDP_PORT%"

set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (
  echo Chrome not found.
  pause
  exit /b 1
)

echo WARN: Starting Chrome with remote debugging on port %PORT%...
echo Uses your normal profile — stay logged in to Seller Central.
echo.
echo Keep this Chrome open, then run Amazon Seller Download with:
echo   AMAZON_ALLOW_UNSAFE_CDP=1
echo   AMAZON_CHROME_CDP_URL=http://127.0.0.1:%PORT%
echo.

start "" "%CHROME%" --remote-debugging-port=%PORT% --remote-debugging-address=127.0.0.1 --remote-allow-origins=* "https://sellercentral.amazon.com/home"

exit /b 0
