@echo off
setlocal
cd /d "%~dp0"

echo ==================================================
echo   Remove Windows Autostart
echo ==================================================
echo.

net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Please right-click this file and "Run as administrator"
    pause
    exit /b 1
)

set "TASK=BinanceTGTrader"

schtasks /query /tn "%TASK%" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Task "%TASK%" not found, nothing to remove
    pause
    exit /b 0
)

schtasks /end /tn "%TASK%" >nul 2>&1
schtasks /delete /tn "%TASK%" /f

if errorlevel 1 (
    echo [ERROR] Failed to remove task
) else (
    echo.
    echo [OK] Autostart removed
)
pause
