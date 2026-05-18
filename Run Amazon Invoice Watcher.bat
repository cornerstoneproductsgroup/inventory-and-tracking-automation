@echo off
setlocal
cd /d "%~dp0"
call "%~dp0invoice report\run_amazon_invoice_watcher.bat" %*
