@echo off
setlocal
set "TASK_NAME=Cornerstone Amazon Invoice Watcher"

echo Removing scheduled task: %TASK_NAME%
schtasks /End /TN "%TASK_NAME%" 2>nul
schtasks /Delete /TN "%TASK_NAME%" /F
if errorlevel 1 (
  echo Task was not found or could not be deleted.
  pause
  exit /b 1
)
echo Done.
pause
exit /b 0
