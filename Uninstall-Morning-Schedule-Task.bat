@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "TASK_NAME=Cornerstone Morning Automation"
set "CONFIG=%~dp0scheduled_workflow.json"

if exist "%CONFIG%" (
  for /f "delims=" %%T in ('powershell -NoProfile -Command "(Get-Content -Raw -LiteralPath '%CONFIG%' | ConvertFrom-Json).schedule.task_name"') do set "TASK_NAME=%%T"
)

echo Removing scheduled task: %TASK_NAME%
schtasks /Delete /TN "%TASK_NAME%" /F
if errorlevel 1 (
  echo Task was not found or could not be removed.
) else (
  echo Removed.
)
echo.
pause
exit /b 0
