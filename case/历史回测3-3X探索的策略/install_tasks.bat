@echo off
chcp 65001 >nul
setlocal
REM ===================================================================
REM  Install Windows scheduled tasks for the BTC strategy.
REM  Just double-click this file. It will request Administrator rights
REM  automatically (a UAC window pops up - click YES).
REM
REM  It creates:
REM    BTC_Signal_Push  - daily 08:05  run_daily.bat  (signal email)
REM    BTC_Price_Alert  - hourly        run_alert.bat  (risk alert email)
REM  It does NOT create an auto-trade task. That is added later on purpose.
REM
REM  NOTE: no parenthesised if-blocks, CJK folder names break cmd there.
REM ===================================================================
cd /d "%~dp0"

REM --- step 1: if not elevated, relaunch self as administrator ---
net session >nul 2>&1
if not errorlevel 1 goto is_admin

echo.
echo   This script needs Administrator rights.
echo   A UAC window will pop up now - click YES.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b 0

:is_admin
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

echo.
echo ==========================================================
echo   BTC strategy - install scheduled tasks
echo   (running as Administrator)
echo ==========================================================
echo.
echo   Folder: %HERE%
echo.
echo   About to create:
echo     [1] BTC_Signal_Push   daily 08:05   -^> run_daily.bat
echo     [2] BTC_Price_Alert   hourly        -^> run_alert.bat
echo     [3] BTC_Auto_Trade    daily 08:10   -^> run_trade.bat
echo.
echo   BTC daily candles close at 08:00 Beijing time, so we run at 08:05.
echo   Task [3] runs 5 min later so the signal is already computed.
echo   Task [3] is SAFE to install now: it does nothing while
echo   _master_switch.mode = observe. It only starts placing real
echo   orders after you run run_switch.bat and type YES.
echo.
pause

echo.
echo --- [1/3] Creating BTC_Signal_Push (daily 08:05) ---
schtasks /Create /F /TN "BTC_Signal_Push" /TR "\"%HERE%\run_daily.bat\"" /SC DAILY /ST 08:05
if errorlevel 1 goto fail1
echo     OK

echo.
echo --- [2/3] Creating BTC_Price_Alert (hourly) ---
schtasks /Create /F /TN "BTC_Price_Alert" /TR "\"%HERE%\run_alert.bat\"" /SC HOURLY /MO 1
if errorlevel 1 goto fail2
echo     OK

echo.
echo --- [3/3] Creating BTC_Auto_Trade (daily 08:10) ---
schtasks /Create /F /TN "BTC_Auto_Trade" /TR "\"%HERE%\run_trade.bat\"" /SC DAILY /ST 08:10
if errorlevel 1 goto fail3
echo     OK

echo.
echo ==========================================================
echo   DONE - all 3 tasks installed.
echo ==========================================================
echo.
echo   Verify what they will run:
echo.
schtasks /Query /TN "BTC_Signal_Push" /XML | findstr /I "Command Arguments"
schtasks /Query /TN "BTC_Price_Alert" /XML | findstr /I "Command Arguments"
schtasks /Query /TN "BTC_Auto_Trade" /XML | findstr /I "Command Arguments"
echo.
echo   Next run times:
echo.
schtasks /Query /TN "BTC_Signal_Push" /FO LIST
echo.
schtasks /Query /TN "BTC_Price_Alert" /FO LIST
echo.
schtasks /Query /TN "BTC_Auto_Trade" /FO LIST
echo.
echo   Test them right now (runs immediately):
echo     schtasks /Run /TN "BTC_Signal_Push"
echo     schtasks /Run /TN "BTC_Price_Alert"
echo     schtasks /Run /TN "BTC_Auto_Trade"
echo.
echo   Note 1: when a task fires you may see a window flash by.
echo           That is normal, not an error.
echo   Note 2: these tasks run when you are logged on. Keep the PC
echo           on and logged in at 08:05 for the daily push to fire.
echo.
pause
exit /b 0

:fail1
echo.
echo [ERROR] Could not create BTC_Signal_Push. Read the message above.
echo         If it still says Access is denied, open Task Scheduler
echo         manually (taskschd.msc) and import the task by hand.
echo.
pause
exit /b 1

:fail2
echo.
echo [ERROR] Could not create BTC_Price_Alert. Read the message above.
echo.
pause
exit /b 1

:fail3
echo.
echo [ERROR] Could not create BTC_Auto_Trade. Read the message above.
echo         The other two tasks were created successfully.
echo.
pause
exit /b 1
