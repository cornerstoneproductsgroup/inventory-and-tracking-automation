@echo off
setlocal
cd /d "%~dp0"
echo Log: %~dp0commercehub_run.log
echo.
REM Menu appears in Python unless you pass 1-4 or "all"/"depot"/"lowes"/"tractor", e.g.:
REM   run_export.bat 2
REM   set COMMERCEHUB_MENU_CHOICE=1
REM Use py -3.13 so a broken global "pip"/"python" shim does not break scheduled runs.
py -3.13 commercehub_invoice_export.py %*
set ERR=%ERRORLEVEL%
echo.
echo Exit code: %ERR%
REM Task Scheduler: use argument nopause  (e.g. run_export.bat nopause)
if /I not "%~1"=="nopause" pause
exit /b %ERR%
