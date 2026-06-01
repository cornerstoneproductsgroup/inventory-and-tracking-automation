@echo off
REM For Windows Task Scheduler — no pause; append to logs\scheduled_workflow.log
setlocal
cd /d "%~dp0"

if not exist "%~dp0logs" mkdir "%~dp0logs"

set "INV_PY=%~dp0Inventory Submissions\.venv\Scripts\python.exe"
set "RUNNER=python"
if exist "%INV_PY%" set "RUNNER=%INV_PY%"

"%RUNNER%" "run_scheduled_workflow.py" >> "%~dp0logs\scheduled_workflow.log" 2>&1
exit /b %ERRORLEVEL%
