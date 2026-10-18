@echo off
setlocal
title BTC Strategy - Daily Check
cd /d "%~dp0"

rem ============================================================================
rem  BTC Strategy - Daily Check  (double-click to run)
rem
rem  Runs btc_monitor.py: checks the single scheme, prints a position report
rem  (entry price / current price / floating PnL) and sends the report email.
rem
rem  Optional arguments (pass through to python):
rem     silent       skip the final "pause"  (used by Task Scheduler)
rem     --no-mail    check only, do not send email
rem     --refresh    force a fresh download of market data
rem     --live       allow real orders (also needs binance.enabled = true)
rem
rem  All Chinese output comes from btc_monitor.py (Python 3.6+ prints Unicode
rem  via WriteConsoleW, so it is independent of the console code page).
rem  Keep this file pure ASCII and CRLF - cmd parses .bat as system ANSI.
rem
rem  The line below is a MARKER. The installer looks for it to recognise this
rem  file as "the daily check script". Do not remove or edit it.
rem  BTCSTRAT_DAILY_CHECK
rem ============================================================================

set "SILENT=0"
if /i "%~1"=="silent" set "SILENT=1"

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
    echo.
    echo Please install Python 3.8+ from https://www.python.org/downloads/
    echo Remember to check "Add Python to PATH" during installation.
    echo.
    if "%SILENT%"=="0" pause
    exit /b 1
)

rem ---- 2. check that the script sits next to this .bat ------------------------
if not exist "%~dp0btc_monitor.py" (
    echo [ERROR] btc_monitor.py not found in the same folder as this .bat
    echo Please keep "btc_monitor.py" and this .bat in the same directory.
    echo.
    if "%SILENT%"=="0" pause
    exit /b 1
)

echo ================================================================================
echo  BTC Strategy - Daily Check
echo  Python  : %PY%
echo  Time    : %DATE% %TIME%
echo ================================================================================
echo.

rem ---- 3. run the check -------------------------------------------------------
rem  -S skips the "site" module, which silences a harmless Python-2 style
rem  sitecustomize.py warning on some Windows Python installs.
rem  btc_monitor.py only uses the standard library, so -S is safe.
rem
rem  IMPORTANT: whether -S works is probed with a ONE-LINE python command,
rem  never by re-running the real script. An earlier version retried the whole
rem  script without -S whenever the -S attempt returned non-zero; if that first
rem  attempt had already sent the report email, the retry sent it AGAIN and the
rem  user got two identical emails. The script is now run EXACTLY ONCE.
set "PYFLAG=-S"
%PY% -S -c "import sys" >nul 2>&1
if errorlevel 1 (
    set "PYFLAG="
    echo [note] -S is not usable on this Python, using the normal interpreter.
)

%PY% %PYFLAG% "%~dp0btc_monitor.py" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo ================================================================================
    echo  DONE - check finished successfully.
    echo  Full log: monitor_log.txt    Latest state: monitor_state.json
    echo ================================================================================
) else (
    echo ================================================================================
    echo  FAILED with exit code %RC% - see monitor_log.txt for details.
    echo ================================================================================
)

if "%SILENT%"=="0" pause
endlocal & exit /b %RC%
