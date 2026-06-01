@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "TASK_NAME=Cornerstone Morning Automation"
set "RUNNER=%~dp0Run Scheduled Workflow (Silent).bat"
set "CONFIG=%~dp0scheduled_workflow.json"
set "SCHEDULE_TIME=05:00"

echo.
echo Installs a Windows scheduled task for the morning automation chain:
echo   Pull Orders -^> FedEx Batch -^> Invoice Reports -^> Inventories
echo.
echo Task name:  %TASK_NAME%
echo Runs:       %RUNNER%
echo Default time: %SCHEDULE_TIME% local ^(override in scheduled_workflow.json or SCHEDULED_WORKFLOW_TIME^)
echo.
echo Requires you to be logged in to Windows ^(browser automation^).
echo Edit steps in scheduled_workflow.json before or after install.
echo.

if not exist "%RUNNER%" (
  echo ERROR: Runner not found:
  echo   %RUNNER%
  pause
  exit /b 1
)

if not exist "%CONFIG%" (
  echo ERROR: Config not found:
  echo   %CONFIG%
  pause
  exit /b 1
)

set "INV_PY=%~dp0Inventory Submissions\.venv\Scripts\python.exe"
if not exist "%INV_PY%" (
  echo WARNING: No venv at Inventory Submissions\.venv
  echo Run Install-Deps.bat first or the task may fail.
  echo.
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference = 'Stop';" ^
  "$configPath = '%CONFIG%';" ^
  "$cfg = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json;" ^
  "$taskName = if ($cfg.schedule.task_name) { $cfg.schedule.task_name } else { '%TASK_NAME%' };" ^
  "$timeRaw = if ($env:SCHEDULED_WORKFLOW_TIME) { $env:SCHEDULED_WORKFLOW_TIME.Trim() } elseif ($cfg.schedule.time_local) { $cfg.schedule.time_local } else { '%SCHEDULE_TIME%' };" ^
  "$parts = $timeRaw -split ':';" ^
  "$hour = [int]$parts[0]; $minute = if ($parts.Length -gt 1) { [int]$parts[1] } else { 0 };" ^
  "$at = (Get-Date).Date.AddHours($hour).AddMinutes($minute);" ^
  "$runner = '%RUNNER%';" ^
  "$workDir = '%~dp0'.TrimEnd('\');" ^
  "$action = New-ScheduledTaskAction -Execute $runner -WorkingDirectory $workDir;" ^
  "$trigger = New-ScheduledTaskTrigger -Daily -At $at;" ^
  "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew;" ^
  "$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited;" ^
  "$desc = 'Morning chain: Pull Orders, FedEx Batch, invoice reports, inventories. Edit scheduled_workflow.json to add steps.';" ^
  "Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description $desc -Force | Out-Null;" ^
  "Write-Host ('Registered task: ' + $taskName) -ForegroundColor Green;" ^
  "Write-Host ('Daily at: ' + $timeRaw + ' local');"

if errorlevel 1 (
  echo.
  echo ERROR: Could not create the scheduled task.
  echo Run this .bat from the repo folder as your normal Windows user.
  pause
  exit /b 1
)

echo.
echo Log file: %~dp0logs\scheduled_workflow.log
echo.
echo Test now ^(optional^): schtasks /Run /TN "%TASK_NAME%"
echo Remove later: Uninstall-Morning-Schedule-Task.bat
echo Open Task Scheduler: taskschd.msc
echo.
pause
exit /b 0
