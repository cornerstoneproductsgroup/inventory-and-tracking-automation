@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "TASK_NAME=Cornerstone Amazon Invoice Watcher"
set "WATCHER_DIR=%~dp0invoice report"
set "RUNNER=%WATCHER_DIR%\run_amazon_invoice_watcher_silent.bat"

echo.
echo Installs a Windows scheduled task so the Amazon invoice watcher starts at logon
echo and restarts if it stops. Uses your Windows login (needed for the share + Excel print).
echo.
echo Task name: %TASK_NAME%
echo Runs:      %RUNNER%
echo.

if not exist "%RUNNER%" (
  echo ERROR: Watcher script not found:
  echo   %RUNNER%
  pause
  exit /b 1
)

set "INV_PY=%~dp0Inventory Submissions\.venv\Scripts\python.exe"
set "IR_PY=%WATCHER_DIR%\.venv\Scripts\python.exe"
if not exist "%IR_PY%" if not exist "%INV_PY%" (
  echo WARNING: No .venv found. Create one and run Install-Deps.bat first, or the task may fail.
  echo   %INV_PY%
  echo.
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference = 'Stop';" ^
  "$taskName = '%TASK_NAME%';" ^
  "$runner = '%RUNNER%';" ^
  "$workDir = '%WATCHER_DIR%';" ^
  "$action = New-ScheduledTaskAction -Execute $runner -WorkingDirectory $workDir;" ^
  "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME;" ^
  "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew;" ^
  "$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited;" ^
  "$desc = 'Watches the Amazon invoice share; formats and prints new exports. Extend amazon_invoice_watcher.py for more steps.';" ^
  "Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description $desc -Force | Out-Null;" ^
  "Write-Host 'Registered scheduled task:' $taskName -ForegroundColor Green;"

if errorlevel 1 (
  echo.
  echo ERROR: Could not create the task. Try running this .bat as your normal user from the repo folder.
  pause
  exit /b 1
)

echo.
echo Starting the task now...
schtasks /Run /TN "%TASK_NAME%"
if errorlevel 1 (
  echo Could not start immediately; it will run at next logon.
) else (
  echo Task started. Log file:
  echo   %WATCHER_DIR%\amazon_watcher.log
)

echo.
echo To remove later: Uninstall-Amazon-Invoice-Watcher-Task.bat
echo To check status:  taskschd.msc  ^(look for "%TASK_NAME%"^)
echo.
pause
exit /b 0
