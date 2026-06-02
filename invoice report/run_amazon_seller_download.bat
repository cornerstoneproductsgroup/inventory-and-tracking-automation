@echo off
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0..\Inventory Submissions\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo Amazon Seller Central download — ON HOLD ^(see AMAZON_SELLER_DOWNLOAD.md^).
echo.

"%PY%" "%~dp0run_amazon_seller_download.py" %*
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" pause
exit /b %ERR%
