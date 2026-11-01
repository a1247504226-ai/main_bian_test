$ErrorActionPreference = 'Stop'
$dir = $PSScriptRoot
$bat = Join-Path $dir 'run_with_retry.bat'
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/c ""{0}""' -f $bat) -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Once -At ([datetime]::Today.AddMinutes(2)) -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 9999)
Register-ScheduledTask -TaskName 'BTC策略监测器' -Action $action -Trigger $trigger -Force -RunLevel Limited | Out-Null
if ($?) {
    Write-Output '计划任务已注册：BTC策略监测器（每小时 :02）'
    exit 0
} else {
    Write-Output '注册失败'
    exit 1
}
