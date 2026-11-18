@echo off
setlocal
title Diagnose duplicate emails - BTC Strategy
cd /d "%~dp0"

rem ============================================================================
rem  DUPLICATE-EMAIL DIAGNOSTIC  (double-click to run)
rem
rem  Read-only. It sends NO email and changes NO state. It only reports:
rem    1. every scheduled task on this PC that runs this strategy
rem    2. every copy of btc_monitor.py on this PC (+ its last run time)
rem    3. the machine-wide mail lock records
rem
rem  Use it when you receive TWO report emails on the same morning.
rem  The report is printed on screen and appended to monitor_log.txt.
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

if not exist "%~dp0btc_monitor.py" (
    echo [ERROR] btc_monitor.py not found in the same folder as this .bat
    echo Keep "btc_monitor.py" and this .bat in the same directory.
    echo.
    pause
    exit /b 1
)

echo.
echo Scanning scheduled tasks and strategy copies, please wait (about 30 seconds) ...
echo.

set "PYFLAG=-S"
%PY% -S -c "import sys" >nul 2>&1
if errorlevel 1 set "PYFLAG="

%PY% %PYFLAG% "%~dp0btc_monitor.py" --doctor

echo.
echo ================================================================================
echo  Done. Full text was also appended to monitor_log.txt
echo ================================================================================
echo.
pause
endlocal
