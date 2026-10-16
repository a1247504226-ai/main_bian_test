@echo off
REM ============================================================================
REM  MACD-X  -  UNINSTALL the daily scheduled task   (uninstall_task.bat)
REM ============================================================================
REM
REM  Removes the Windows Task Scheduler job that runs run_signal.bat every day.
REM
REM  Default task name : MACD-X-Daily
REM  Custom task name  : uninstall_task.bat MyTaskName
REM
REM  THIS SCRIPT ONLY REMOVES THE SCHEDULE. NO FILES ARE DELETED.
REM  macdx_strategy.py / binance_trader.py / run_signal.bat /
REM  signal_state.json / logs\  are all left exactly as they are.
REM
REM  If the delete step fails, close the window, right-click this file and
REM  pick "Run as administrator", then run it again.
REM
REM  WHY THIS FILE IS PURE ASCII:
REM    cmd.exe parses .bat files using the console codepage (GBK on Chinese
REM    Windows). Non-ASCII bytes get mis-aligned and swallow the following
REM    character, producing bogus "not a valid command" errors.
REM ============================================================================

setlocal
set "TASKNAME=MACD-X-Daily"
if not "%~1"=="" set "TASKNAME=%~1"

echo.
echo ============================================================================
echo   MACD-X  -  uninstall daily scheduled task
echo ============================================================================
echo.
echo   Task name : %TASKNAME%
echo.

REM ---------- step 0: list anything that looks like our task ---------------
echo   [0/3] Scheduled tasks currently matching "MACD":
schtasks /Query /FO TABLE | findstr /I "MACD"
if errorlevel 1 echo         none found
echo.

REM ---------- step 1: is the task installed at all? ------------------------
schtasks /Query /TN "%TASKNAME%" >nul 2>&1
if errorlevel 1 (
    echo   [1/3] Task "%TASKNAME%" is NOT installed on this machine.
    echo         Nothing to remove.
    goto :done
)
echo   [1/3] Task found.

REM ---------- step 2: delete it --------------------------------------------
echo   [2/3] Deleting ...
schtasks /Delete /TN "%TASKNAME%" /F >nul 2>&1
if errorlevel 1 (
    echo         [FAIL] Could not delete the task.
    echo         Most likely cause: not enough rights.
    echo         Fix: run this file as administrator, then try again.
    goto :done
)

REM ---------- step 3: verify it is really gone -----------------------------
schtasks /Query /TN "%TASKNAME%" >nul 2>&1
if not errorlevel 1 (
    echo         [FAIL] The task still exists after the delete call.
    goto :done
)
echo   [3/3] Verified - the task is gone.
echo.
echo   The daily auto-run is now OFF. Nothing else was changed.
echo.
echo   Files NOT touched:
echo       macdx_strategy.py
echo       binance_trader.py
echo       run_signal.bat
echo       signal_state.json
echo       logs\
echo.
echo   You can still run the strategy by hand any time:
echo       double-click run_signal.bat
echo.
echo   To install the schedule again, run this in an admin cmd window:
echo       schtasks /Create /TN "%TASKNAME%" /TR "%~dp0run_signal.bat" /SC DAILY /ST 08:01 /F

:done
echo.
echo ============================================================================
echo   Finished.  Press any key to close this window.
echo ============================================================================
pause >nul
endlocal
