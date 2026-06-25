@echo off
setlocal
cd /d "%~dp0"

echo.
echo REMOVED: Remote debugging is not used for Amazon automation.
echo It triggers IT security alerts and is not supported.
echo.
echo Run "Run Amazon Seller Download.bat" instead.
echo.
pause
exit /b 1
