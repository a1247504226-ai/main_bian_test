@echo off
setlocal
cd /d "%~dp0"
set "TASK=GoldTrendStrategy"

echo ============================================================
echo  Install scheduled task: %TASK%   (daily 15:30)
echo ============================================================

rem Register via PowerShell: robust for paths with spaces/Chinese/parentheses
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p = Join-Path (Get-Location) 'run_strategy.bat'; $a = New-ScheduledTaskAction -Execute $p; $t = New-ScheduledTaskTrigger -Daily -At '15:30'; Register-ScheduledTask -TaskName 'GoldTrendStrategy' -Action $a -Trigger $t -Description 'Gold AU trend strategy daily signal' -Force"

schtasks /Query /TN "%TASK%" >nul 2>&1
if errorlevel 1 goto FAIL

echo.
echo [OK] Task "%TASK%" installed. It will run every day at 15:30.
echo      Log file: logs\run.log
echo      You can run run_strategy.bat now to test immediately.
goto END

:FAIL
echo.
echo [FAIL] Could not install the task.
echo        Try: right-click this file -^> "Run as administrator".
goto END

:END
echo.
pause
endlocal
