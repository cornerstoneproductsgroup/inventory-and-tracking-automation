@echo off
setlocal
cd /d "%~dp0"
echo.
echo Amazon Seller download is ON HOLD ^(phone 2FA required every automated run^).
echo.
echo Code is kept under invoice report\ for a future approach.
echo To re-enable later: AMAZON_SELLER_DOWNLOAD_ENABLED=true in invoice report\.env
echo   and add amazon_seller_download back to scheduled_workflow.json steps[].
echo.
echo Manual pipeline still works: drop a CSV in the Amazon Input share and use
echo   Run Amazon Invoice Watcher.bat
echo.
pause
exit /b 0
