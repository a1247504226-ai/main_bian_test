@echo off
setlocal
title BTC Strategy - Account Status
cd /d "%~dp0"

rem ============================================================================
rem  BTC Strategy - Account Status  (double-click to run)
rem
rem  READ-ONLY. Runs btc_trade.py --status:
rem     - connects to Binance and reads balances / positions
rem     - prints the strategy target and how far the account is from it
rem     - places NOTHING, changes NOTHING
rem
rem  Works even when binance.enabled = false, because it never writes.
rem  Needs api_key / api_secret to read a real account.
rem
rem  Optional arguments:
rem     --testnet   read the Binance testnet account instead
rem
rem  Keep this file pure ASCII and CRLF - cmd parses .bat as system ANSI.
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

%PY% %PYFLAG% "%~dp0btc_trade.py" --status %*
set "RC=%ERRORLEVEL%"

echo.
pause
endlocal & exit /b %RC%
