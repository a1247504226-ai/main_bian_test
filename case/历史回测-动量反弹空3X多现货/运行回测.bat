@echo off
setlocal
title BTC Daily Trend Strategy - One-Click Backtest
cd /d "%~dp0"

echo ================================================================================
echo  BTC Daily Trend Strategy - One-Click Backtest
echo  (All report text is printed in Chinese by btc_strategy.py below)
echo ================================================================================
echo.

rem ---- 1. locate a Python interpreter -------------------------------------------
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
    pause
    exit /b 1
)

rem ---- 2. check that the script sits next to this .bat --------------------------
if not exist "%~dp0btc_strategy.py" (
    echo [ERROR] btc_strategy.py not found in the same folder as this .bat
    echo Please keep "btc_strategy.py" and this .bat in the same directory.
    echo.
    pause
    exit /b 1
)

echo [1/2] Python interpreter : %PY%
%PY% --version
echo.

rem ---- 3. run the backtest ------------------------------------------------------
echo [2/2] Running backtest ... (first run downloads market data, about 10-30s)
echo.

rem -S skips the "site" module, which silences a harmless Python-2 style
rem sitecustomize.py warning that some Windows Python installs carry around.
rem The backtest only uses the standard library, so -S is safe here.
%PY% -S "%~dp0btc_strategy.py"
if errorlevel 1 (
    echo.
    echo [note] -S mode failed, retrying with the normal interpreter ...
    %PY% "%~dp0btc_strategy.py"
)
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo ================================================================================
    echo  SUCCESS - all comparison data has been written to the output folder
    echo  printed above. Open those .csv files directly in Excel.
    echo ================================================================================
) else (
    echo ================================================================================
    echo  FAILED with exit code %RC% - please screenshot the error above and report it.
    echo ================================================================================
)

echo.
echo Tip: to force a fresh download of the latest market data, run:
echo        python btc_strategy.py --refresh
echo.
pause
endlocal
