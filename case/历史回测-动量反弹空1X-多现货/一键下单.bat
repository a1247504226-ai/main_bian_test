@echo off
setlocal
title BTC Strategy - Place Orders
cd /d "%~dp0"

rem ============================================================================
rem  BTC Strategy - Place Orders  (double-click to run)
rem
rem  Two steps, by design:
rem     STEP 1  dry run  -> prints exactly what it WOULD do, places nothing
rem     STEP 2  asks you -> only if you type Y does it send real orders
rem
rem  Hard gates inside btc_trade.py (all must pass, otherwise dry run only):
rem     (1) monitor_config.json  binance.enabled = true
rem     (2) binance.api_key / api_secret filled in
rem     (3) this script passes --live
rem     (4) no other process is trading (machine-level lock)
rem     (5) account readable and balance sufficient
rem
rem  Optional arguments:
rem     --force     ignore "already executed this daily bar" hint
rem     --testnet   force the Binance testnet (recommended for the first try)
rem
rem  Keep this file pure ASCII and CRLF - cmd parses .bat as system ANSI.
rem  All Chinese output comes from btc_trade.py.
rem ============================================================================

rem ---- 1. locate a Python interpreter ----------------------------------------
set "PY="

py -3 --version >nul 2>&1
if not errorlevel 1 set "PY=py -3"

if not defined PY (
    python --version >nul 2>&1
    if not errorlevel 1 set "PY=python"
)

if not defined PY (
    for /f "delims=" %%D in ('dir /b /o-n "%LOCALAPPDATA%\Programs\Python\Python*" 2^>nul') do (
        if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\%%D\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\%%D\python.exe"
    )
)

if not defined PY (
    for /f "delims=" %%D in ('dir /b /o-n "C:\Python*" 2^>nul') do (
        if not defined PY if exist "C:\%%D\python.exe" set "PY=C:\%%D\python.exe"
    )
)

if not defined PY (
    echo [ERROR] Python interpreter not found.
    echo Please install Python 3.8+ from https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

if not exist "%~dp0btc_trade.py" (
    echo [ERROR] btc_trade.py not found in the same folder as this .bat
    echo.
    pause
    exit /b 1
)

set "PYFLAG=-S"
%PY% -S -c "import sys" >nul 2>&1
if errorlevel 1 set "PYFLAG="

echo ================================================================================
echo  BTC Strategy - Place Orders
echo  Python : %PY%
echo  Time   : %DATE% %TIME%
echo ================================================================================
echo.

rem ---- 2. STEP 1: dry run (never places anything) ----------------------------
echo [STEP 1/2] Dry run - nothing will be sent to the exchange.
echo.
%PY% %PYFLAG% "%~dp0btc_trade.py" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo ================================================================================
    echo  Dry run failed with exit code %RC% - see the messages above.
    echo  Nothing was placed. Fix the problem and run again.
    echo ================================================================================
    pause
    exit /b %RC%
)

echo.
echo --------------------------------------------------------------------------------
echo  Above is the PLAN ONLY. Nothing has been sent yet.
echo --------------------------------------------------------------------------------
echo.
set "ANS="
set /p "ANS=Type Y then Enter to place REAL orders, or just Enter to cancel: "
if /i not "%ANS%"=="Y" (
    echo.
    echo Cancelled - no order was placed.
    echo.
    pause
    exit /b 0
)

rem ---- 3. STEP 2: real orders ------------------------------------------------
echo.
echo [STEP 2/2] Placing real orders ...
echo.
%PY% %PYFLAG% "%~dp0btc_trade.py" --live %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo ================================================================================
    echo  DONE - position reconciled with the strategy target.
    echo  Trade log   : trade_log.csv
    echo  Last snapshot: trade_state.json
    echo ================================================================================
) else (
    echo ================================================================================
    echo  FAILED with exit code %RC% - check trade_log.csv and the messages above.
    echo  Re-running is safe: it reconciles target vs actual, it does not double up.
    echo ================================================================================
)

pause
endlocal & exit /b %RC%
