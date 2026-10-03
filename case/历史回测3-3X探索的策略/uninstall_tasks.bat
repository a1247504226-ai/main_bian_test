@echo off
chcp 65001 >nul
setlocal
REM ===================================================================
REM  Remove the Windows scheduled tasks created by install_tasks.bat
REM  Note: this only removes the SCHEDULE. It does not close positions
REM        and does not touch config.json.
REM  NOTE: no parenthesised if-blocks, CJK folder names break cmd there.
REM ===================================================================
cd /d "%~dp0"

echo.
echo ==========================================================
echo   BTC strategy - REMOVE scheduled tasks
echo ==========================================================
echo.
echo   This will delete these tasks on this machine:
echo     BTC_Signal_Push
echo     BTC_Price_Alert
echo     BTC_Auto_Trade   (if it exists)
echo.
echo   It will NOT close your Binance positions.
echo   It will NOT change config.json.
echo.
pause

schtasks /Delete /F /TN "BTC_Signal_Push" 2>nul
schtasks /Delete /F /TN "BTC_Price_Alert" 2>nul
schtasks /Delete /F /TN "BTC_Auto_Trade" 2>nul

echo.
echo ==========================================================
echo   DONE. Tasks removed (any that existed).
echo ==========================================================
echo.
pause
exit /b 0
