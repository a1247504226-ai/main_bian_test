@echo off
chcp 65001 >nul
setlocal
REM ===================================================================
REM  Install Windows scheduled tasks for the BTC strategy.
REM  Double-click this file, then confirm the UAC prompt if it appears.
REM  It creates:
REM    BTC_Signal_Push  - daily 08:05  run_daily.bat  (signal email)
REM    BTC_Price_Alert  - hourly        run_alert.bat  (risk alert email)
REM  It does NOT create an auto-trade task (that is done later, on purpose).
REM  NOTE: no parenthesised if-blocks, CJK folder names break cmd there.
REM ===================================================================
cd /d "%~dp0"

set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

echo.
echo ==========================================================
echo   BTC strategy - install scheduled tasks
echo ==========================================================
echo.
echo   Folder : %HERE%
echo.
echo   About to create:
echo     [1] BTC_Signal_Push   every day at 08:05   -^> run_daily.bat
echo     [2] BTC_Price_Alert   every hour           -^> run_alert.bat
echo.
echo   BTC daily candles close at 08:00 Beijing time.
echo   We run at 08:05 so the data is settled.
echo.
echo   This script needs Administrator rights.
echo   If a UAC window pops up, click Yes.
echo.
pause

echo.
echo --- Creating BTC_Signal_Push (daily 08:05) ---
schtasks /Create /F /TN "BTC_Signal_Push" /TR "\"%HERE%\run_daily.bat\"" /SC DAILY /ST 08:05 /RL HIGHEST
if errorlevel 1 goto task1_fail
echo     OK

echo.
echo --- Creating BTC_Price_Alert (hourly) ---
schtasks /Create /F /TN "BTC_Price_Alert" /TR "\"%HERE%\run_alert.bat\"" /SC HOURLY /MO 1 /RL HIGHEST
if errorlevel 1 goto task2_fail
echo     OK

echo.
echo ==========================================================
echo   DONE. Both tasks installed.
echo ==========================================================
echo.
echo   Verify any time with:
echo     schtasks /Query /TN "BTC_Signal_Push" /V /FO LIST
echo     schtasks /Query /TN "BTC_Price_Alert" /V /FO LIST
echo.
echo   Test them right now (runs immediately):
echo     schtasks /Run /TN "BTC_Signal_Push"
echo     schtasks /Run /TN "BTC_Price_Alert"
echo.
echo   Remove them later:
echo     schtasks /Delete /F /TN "BTC_Signal_Push"
echo     schtasks /Delete /F /TN "BTC_Price_Alert"
echo.
echo   Note: the scheduled task runs even if the console window
echo   flashes by. That is normal - it is not an error.
echo.
pause
exit /b 0

:task1_fail
echo.
echo [ERROR] Failed to create BTC_Signal_Push.
echo         Most likely cause: this window is not running as Admin.
echo         Fix: right-click this .bat -^> "Run as administrator".
echo.
pause
exit /b 1

:task2_fail
echo.
echo [ERROR] Failed to create BTC_Price_Alert.
echo         Most likely cause: this window is not running as Admin.
echo         Fix: right-click this .bat -^> "Run as administrator".
echo.
pause
exit /b 1
