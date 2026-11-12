@echo off
setlocal
cd /d "%~dp0"

echo ==================================================
echo   Install Windows Autostart
echo ==================================================
echo.

:: need admin
net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Please right-click this file and "Run as administrator"
    pause
    exit /b 1
)

:: find pythonw (runs without console window)
set "PYW="
for /f "delims=" %%i in ('where pythonw 2^>nul') do (
    if not defined PYW set "PYW=%%i"
)
if not defined PYW (
    echo [ERROR] pythonw.exe not found in PATH
    echo         Make sure Python 3.9+ is installed and added to PATH
    pause
    exit /b 1
)

set "APP_DIR=%~dp0"
set "TASK=BinanceTGTrader"

echo Python:  %PYW%
echo Project: %APP_DIR%
echo.

:: remove old task first
schtasks /delete /tn "%TASK%" /f >nul 2>&1

:: create task: run at user logon, background, no window
schtasks /create /tn "%TASK%" ^
    /tr "\"%PYW%\" \"%APP_DIR%trader.py\" --live" ^
    /sc onlogon /rl highest /f

if errorlevel 1 (
    echo.
    echo [ERROR] Failed to create scheduled task
    pause
    exit /b 1
)

echo.
echo ==================================================
echo   Done
echo ==================================================
echo.
echo   Task name : %TASK%
echo   Trigger   : at user logon
echo   Mode      : background (no window), LIVE orders on testnet
echo   Log file  : %APP_DIR%logs\trader.log
echo.
echo   Commands:
echo     schtasks /run    /tn "%TASK%"     start now
echo     schtasks /end    /tn "%TASK%"     stop
echo     schtasks /query  /tn "%TASK%"     check status
echo     uninstall_autostart.bat           remove
echo.
echo   NOTE: log is written to logs\trader.log (daily rotation, keep 14 days)
echo ==================================================
pause
