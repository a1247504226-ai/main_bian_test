$ErrorActionPreference = 'Stop'
$dir = $PSScriptRoot
$bat = Join-Path $dir 'run_with_retry.bat'

$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/c ""{0}""' -f $bat) -WorkingDirectory $dir

# 触发点对齐到「下一个整点 :02」，之后每小时 :02 触发。
# 这样每日 08:02 那轮必定落在 daily_email_hour=8 内，邮件才会准点。
$now   = Get-Date
$start = $now.Date.AddHours($now.Hour).AddMinutes(2)
if ($start -le $now) { $start = $start.AddHours(1) }

$trigger = New-ScheduledTaskTrigger -Once -At $start `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 9999)

# StartWhenAvailable：错过触发（休眠/关机）后开机补跑
# 电池模式也跑；执行超时 30 分钟防止卡死；同时只允许一个实例
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName 'BTC策略监测器' -Action $action -Trigger $trigger `
    -Settings $settings -Force -RunLevel Limited | Out-Null

if ($?) {
    Write-Output ("计划任务已注册：BTC策略监测器（每小时 :02，首次 {0:yyyy-MM-dd HH:mm}）" -f $start)
    exit 0
} else {
    Write-Output '注册失败'
    exit 1
}
