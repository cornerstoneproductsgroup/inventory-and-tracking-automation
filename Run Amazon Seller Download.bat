@echo off
setlocal
cd /d "%~dp0"
call "invoice report\run_amazon_seller_download.bat" %*
