@echo off
setlocal
cd /d "%~dp0"
title Binance TG Trader

echo ==================================================
echo    Binance TG Auto Trader
echo ==================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python not found in PATH
    echo         Install Python 3.9+ first
    pause
    exit /b 1
)

if not exist ".env.bot" (
    echo [ERROR] .env.bot missing
    echo         Copy .env.bot.example to .env.bot and fill in keys
    pause
    exit /b 1
)

python -c "import requests, yaml, dotenv" >nul 2>nul
if errorlevel 1 (
    echo [INFO] Installing dependencies...
    python -m pip install -q -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] pip install failed
        pause
        exit /b 1
    )
)

echo Select run mode:
echo.
echo   [1] Testnet + dry-run        (safest, parse only)
echo   [2] Testnet + live orders    (virtual money)
echo   [3] Replay history file      (offline test)
echo   [4] Check config
echo   [5] MAINNET + live orders    (REAL MONEY)
echo.
set "choice="
set /p "choice=Enter number [1]: "
if "%choice%"=="" set "choice=1"

if "%choice%"=="1" goto M1
if "%choice%"=="2" goto M2
if "%choice%"=="3" goto M3
if "%choice%"=="4" goto M4
if "%choice%"=="5" goto M5
echo Invalid choice
pause
exit /b 1

:M1
echo.
echo === Testnet + dry-run ===
python trader.py
goto END

:M2
echo.
echo === Testnet + live orders ===
python trader.py --live
goto END

:M3
echo.
set "f="
set /p "f=History file path (Enter for default): "
if "%f%"=="" set "f=D:\Down\cyclone\telegram_history_10000.txt"
if not exist "%f%" (
    echo [ERROR] file not found: %f%
    goto END
)
echo === Replay: %f% ===
python trader.py --replay "%f%" --limit 50
goto END

:M4
echo.
python main.py check
goto END

:M5
echo.
echo ##################################################
echo   WARNING: REAL MONEY on Binance MAINNET
echo ##################################################
echo.
set "c="
set /p "c=Type YES to continue: "
if /i not "%c%"=="YES" (
    echo Cancelled
    goto END
)
python trader.py --mainnet --live
goto END

:END
echo.
echo ==================================================
echo Done
echo ==================================================
pause
