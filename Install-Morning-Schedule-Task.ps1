# Register daily morning automation task (called from Install-Morning-Schedule-Task.bat).
$ErrorActionPreference = 'Stop'

$repoRoot = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$configPath = Join-Path $repoRoot 'scheduled_workflow.json'
$runner = Join-Path $repoRoot 'Run-Scheduled-Workflow-Silent.bat'
$defaultTaskName = 'Cornerstone Morning Automation'
$defaultTime = '05:00'

if (-not (Test-Path -LiteralPath $runner)) {
    Write-Error "Runner not found: $runner"
    exit 1
}
if (-not (Test-Path -LiteralPath $configPath)) {
    Write-Error "Config not found: $configPath`nRun git pull to get scheduled_workflow.json."
    exit 1
}

$cfg = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
$taskName = if ($cfg.schedule.task_name) { [string]$cfg.schedule.task_name } else { $defaultTaskName }
$timeRaw = if ($env:SCHEDULED_WORKFLOW_TIME) {
    $env:SCHEDULED_WORKFLOW_TIME.Trim()
} elseif ($cfg.schedule.time_local) {
    [string]$cfg.schedule.time_local
} else {
    $defaultTime
}

$parts = $timeRaw -split ':'
$hour = [int]$parts[0]
$minute = if ($parts.Length -gt 1) { [int]$parts[1] } else { 0 }
$at = (Get-Date).Date.AddHours($hour).AddMinutes($minute)

$action = New-ScheduledTaskAction -Execute $runner -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $at
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$desc = 'Morning chain: Pull Orders, FedEx Batch, invoice reports, inventories. Edit scheduled_workflow.json to add steps.'

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description $desc `
    -Force | Out-Null

Write-Host "Registered task: $taskName" -ForegroundColor Green
Write-Host "Daily at: $timeRaw local"
Write-Host "Runs: $runner"
exit 0
