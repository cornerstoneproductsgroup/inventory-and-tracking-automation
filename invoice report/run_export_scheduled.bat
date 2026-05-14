@echo off
REM For Windows Task Scheduler — exits immediately with Python exit code (no pause).
REM Default: run all invoicing (1). Override: set COMMERCEHUB_MENU_CHOICE=2 in .env or pass another digit.
call "%~dp0run_export.bat" nopause 1
exit /b %ERRORLEVEL%
