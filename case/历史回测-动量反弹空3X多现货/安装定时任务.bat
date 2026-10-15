@echo off
setlocal
title Install Scheduled Task - BTC Strategy Daily Check
cd /d "%~dp0"

rem ============================================================================
rem  ONE-CLICK INSTALLER  --  Windows Task Scheduler
rem
rem  What it does
rem    1. asks for administrator rights (needed to write a scheduled task)
rem    2. auto-detects the daily-check .bat sitting in this same folder
rem    3. registers a daily task that runs it at 08:05 local time
rem    4. verifies the task and offers a test run
rem
rem  Usage
rem    just double-click it                        -> install at 08:05
rem    or from a command prompt:  install 09:30    -> install at 09:30
rem    (passing a time also works: drag nothing, just call it with an argument)
rem
rem  To remove the task later, run the uninstaller .bat in this folder, or:
rem    schtasks /Delete /TN "BTC_Strategy_DailyCheck" /F
rem
rem  NOTE: keep this file pure ASCII and CRLF.  cmd.exe parses .bat files using
rem  the system ANSI code page, so non-ASCII text in here would be garbled.
rem  All Chinese output comes from the Python script instead.
rem ============================================================================

set "TASKNAME=BTC_Strategy_DailyCheck"
set "RUNAT=%~1"
if "%RUNAT%"=="" set "RUNAT=08:05"

rem ---------------------------------------------------------------- elevation
net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo  Administrator rights are required to create a scheduled task.
    echo  Re-launching with elevation - please click "Yes" on the prompt.
    echo.
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs -ArgumentList '%RUNAT%'"
    exit /b
)

echo ================================================================================
echo  BTC Strategy - Install Daily Scheduled Task
echo ================================================================================
echo.

rem ------------------------------------------------- sanity check this folder
if not exist "%~dp0monitor_config.json" (
    echo  [ERROR] monitor_config.json was not found next to this installer.
    echo          Keep every file of the strategy in the SAME folder.
    echo.
    pause
    exit /b 1
)

rem --------------------------------------------------- locate the check script
rem  The daily-check .bat is recognised by a MARKER inside it, not by file name,
rem  so it keeps working whatever the file is renamed to.
rem  The marker is assembled from two halves on purpose: that way this installer
rem  never contains the marker itself and can never match its own file.
set "MK1=BTCSTRAT"
set "MK2=_DAILY_CHECK"
set "TARGET="
for %%F in ("%~dp0*.bat") do (
    if not defined TARGET (
        if /i not "%%~nxF"=="%~nx0" (
            findstr /m /c:"%MK1%%MK2%" "%%~fF" >nul 2>&1
            if not errorlevel 1 set "TARGET=%%~fF"
        )
    )
)

if not defined TARGET (
    echo  [ERROR] Could not find the daily-check .bat in this folder.
    echo          It has to sit next to this installer.
    echo.
    pause
    exit /b 1
)

echo  Folder       : %~dp0
echo  Check script : %TARGET%
echo  Task name    : %TASKNAME%
echo  Schedule     : every day at %RUNAT%  (local time)
echo.
echo  Registering the task ...
echo.

set "TR=cmd /c ""%TARGET%" silent"
schtasks /Create /F /TN "%TASKNAME%" /TR "%TR%" /SC DAILY /ST %RUNAT%

if errorlevel 1 (
    echo.
    echo  [ERROR] Failed to create the task.
    echo          Right-click this file and choose "Run as administrator", then retry.
    echo.
    pause
    exit /b 1
)

echo.
echo  Verifying the registration ...
echo.
schtasks /Query /TN "%TASKNAME%" /FO LIST | findstr /i "TaskName Next Status"

rem ---- pin the advanced settings ---------------------------------------------
rem  The defaults schtasks writes vary between Windows builds.  Set the ones we
rem  actually care about explicitly, so the behaviour is the same everywhere:
rem    StartWhenAvailable  -> if 08:05 was missed (PC asleep / off), run on wake
rem    ExecutionTimeLimit  -> kill a hung run after 1 hour
rem    MultipleInstances   -> never run two copies at once
rem  If PowerShell is unavailable we just carry on - the task already exists.
echo.
echo  Applying advanced settings ...
powershell -NoProfile -Command "$n='%TASKNAME%'; $s=(Get-ScheduledTask -TaskName $n).Settings; $s.StartWhenAvailable=$true; $s.ExecutionTimeLimit='PT1H'; $s.MultipleInstances='IgnoreNew'; $s.DisallowStartIfOnBatteries=$false; $s.StopIfGoingOnBatteries=$false; Set-ScheduledTask -TaskName $n -Settings $s | Out-Null" >nul 2>&1
if errorlevel 1 (
    echo  [warn] Could not apply advanced settings - the task still works,
    echo         but a missed run may not be retried. Details are in the docs.
) else (
    echo  OK - missed runs are retried, hung runs are killed after 1 hour.
)

echo.
echo ================================================================================
echo  SUCCESS - the daily task is installed.
echo ================================================================================
echo  Verify :  schtasks /Query /TN "%TASKNAME%" /V /FO LIST
echo  Run now:  schtasks /Run   /TN "%TASKNAME%"
echo  Remove :  run the uninstaller .bat in this folder
echo ================================================================================
echo.

set "RUNNOW="
set /p RUNNOW=Run the check once right now to verify? [Y/N]
if /i "%RUNNOW%"=="Y" (
    echo.
    schtasks /Run /TN "%TASKNAME%"
    echo  Task started in the background.
    echo  Give it about 30 seconds, then open monitor_log.txt to see the result.
)

echo.
pause
endlocal
