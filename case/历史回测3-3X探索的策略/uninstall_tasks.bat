@echo off
chcp 65001 >nul
setlocal
REM ===================================================================
REM  Remove the Windows scheduled tasks created by install_tasks.bat
REM  Just double-click. It will request Administrator rights itself.
REM
REM  This only removes the SCHEDULE. It does NOT close positions
REM  and does NOT touch config.json.
REM
REM  NOTE: no parenthesised if-blocks, CJK folder names break cmd there.
REM ===================================================================
cd /d "%~dp0"

net session >nul 2>&1
if not errorlevel 1 goto is_admin

echo.
echo   This script needs Administrator rights.
echo   A UAC window will pop up now - click YES.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b 0

:is_admin

echo.
echo ==========================================================
echo   BTC strategy - REMOVE scheduled tasks
echo   (running as Administrator)
echo ==========================================================
echo.
echo   Will delete these tasks on this machine:
echo     BTC_Signal_Push
echo     BTC_Price_Alert
echo     BTC_Auto_Trade   (only if it exists)
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
echo   DONE. Remaining BTC tasks on this machine:
echo ==========================================================
echo.
schtasks /Query /FO TABLE | findstr /I "BTC_"
echo.
pause
exit /b 0
