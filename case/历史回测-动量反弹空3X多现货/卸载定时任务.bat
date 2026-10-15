@echo off
setlocal
title Uninstall Scheduled Task - BTC Strategy Daily Check
cd /d "%~dp0"

rem ============================================================================
rem  ONE-CLICK UNINSTALLER  --  Windows Task Scheduler
rem
rem  Removes the scheduled task "BTC_Strategy_DailyCheck".
rem  It ONLY removes the task - no file in this folder is ever deleted.
rem
rem  NOTE: keep this file pure ASCII and CRLF (see the installer for why).
rem ============================================================================

set "TASKNAME=BTC_Strategy_DailyCheck"

rem ---------------------------------------------------------------- elevation
net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo  Administrator rights are required to remove a scheduled task.
    echo  Re-launching with elevation - please click "Yes" on the prompt.
    echo.
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo ================================================================================
echo  BTC Strategy - Remove Daily Scheduled Task
echo ================================================================================
echo.

schtasks /Query /TN "%TASKNAME%" >nul 2>&1
if errorlevel 1 (
    echo  Task "%TASKNAME%" was not found - nothing to remove.
    echo  (It may already be uninstalled, or never installed on this machine.)
    echo.
    pause
    exit /b 0
)

echo  Found this task:
echo.
schtasks /Query /TN "%TASKNAME%" /FO LIST | findstr /i "TaskName Next Status"
echo.

set "YES="
set /p YES=Remove it? [Y/N]
if /i not "%YES%"=="Y" (
    echo.
    echo  Cancelled - nothing was changed.
    echo.
    pause
    exit /b 0
)

echo.
schtasks /Delete /TN "%TASKNAME%" /F

if errorlevel 1 (
    echo.
    echo  [ERROR] Failed to remove the task.
    echo          Right-click this file and choose "Run as administrator", then retry.
    echo.
    pause
    exit /b 1
)

echo.
echo ================================================================================
echo  SUCCESS - the daily task has been removed.
echo.
echo  No files were deleted. The strategy folder is untouched and you can still
echo  run the daily check by hand any time. Re-install with the installer .bat.
echo ================================================================================
echo.
pause
endlocal
