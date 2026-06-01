@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "TASK_NAME=Cornerstone Morning Automation"
set "RUNNER=%~dp0Run-Scheduled-Workflow-Silent.bat"
set "CONFIG=%~dp0scheduled_workflow.json"
set "PS1=%~dp0Install-Morning-Schedule-Task.ps1"

echo.
echo Installs a Windows scheduled task for the morning automation chain:
echo   Pull Orders -^> FedEx Batch -^> Invoice Reports -^> Inventories
echo.
echo Task name:  %TASK_NAME%
echo Runs:       %RUNNER%
echo.
echo Requires you to be logged in to Windows ^(browser automation^).
echo Edit steps in scheduled_workflow.json before or after install.
echo.

if not exist "%RUNNER%" goto ERR_NO_RUNNER
if not exist "%CONFIG%" goto ERR_NO_CONFIG
if not exist "%PS1%" goto ERR_NO_PS1
goto CHECK_VENV

:ERR_NO_RUNNER
echo ERROR: Runner not found:
echo   %RUNNER%
echo.
echo If you just pulled the repo, confirm Run-Scheduled-Workflow-Silent.bat exists.
goto FAIL

:ERR_NO_CONFIG
echo ERROR: Config not found:
echo   %CONFIG%
echo.
echo Run: git pull origin main
goto FAIL

:ERR_NO_PS1
echo ERROR: Missing installer script:
echo   %PS1%
echo.
echo Run: git pull origin main
goto FAIL

:CHECK_VENV
set "INV_PY=%~dp0Inventory Submissions\.venv\Scripts\python.exe"
if exist "%INV_PY%" goto RUN_INSTALL
echo WARNING: No venv at Inventory Submissions\.venv
echo Run Inventory Submissions\Install-Deps.bat first or the task may fail.
echo.

:RUN_INSTALL
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
if errorlevel 1 goto FAIL

echo.
echo Log file: %~dp0logs\scheduled_workflow.log
echo.
echo Test now ^(optional^): schtasks /Run /TN "%TASK_NAME%"
echo Remove later: Uninstall-Morning-Schedule-Task.bat
echo Open Task Scheduler: taskschd.msc
echo.
pause
exit /b 0

:FAIL
echo.
echo Install failed. See messages above.
echo.
pause
exit /b 1
