# Register the daily pre-market signal task.
# Paths are taken from $PSScriptRoot, so spaces / Chinese in the folder name are safe.
$ErrorActionPreference = "Stop"
$dir = $PSScriptRoot
$bat = Join-Path $dir "daily_signal_runner.bat"
# Prefer the Chinese-named runner if present, else fall back.
$cnBat = Join-Path $dir ([string]::Concat([char]0x6BCF,[char]0x65E5,[char]0x4FE1,[char]0x53F7,".bat"))
if (Test-Path $cnBat) { $bat = $cnBat }

$taskName = "US_Trend_Signal_Daily"

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$action  = New-ScheduledTaskAction -Execute $bat -Argument "--quiet" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Daily -At "20:30"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "US stock MA200 trend signal - daily pre-market mail" -Force | Out-Null

$t = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Host ""
Write-Host "[OK] Scheduled task registered: $taskName"
Write-Host "     Action : $($t.Actions[0].Execute) --quiet"
Write-Host "     Time   : daily 20:30 (Beijing)"
Write-Host "     NextRun: $($info.NextRunTime)"
Write-Host ""
Write-Host "The signal will now run automatically every day at 20:30, no clicking needed."
