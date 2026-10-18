@echo off
REM ============================================================
REM  US Trend Signal - daily pre-market mail
REM  Double-click to run (window stays open).
REM  Task Scheduler calls:  run with  --quiet
REM ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

REM ---- locate python interpreter ----
set "PYEXE="
if exist "%~dp0signal\python_exe.txt" (
  for /f "usebackq delims=" %%i in ("%~dp0signal\python_exe.txt") do set "PYEXE=%%i"
)
if not defined PYEXE set "PYEXE=python"

if not exist "%~dp0signal\logs" mkdir "%~dp0signal\logs"

REM ---- strip --quiet from forwarded args ----
set "ARGS=%*"
set "ARGS=%ARGS:--quiet=%"

"%PYEXE%" "%~dp0signal\daily_signal.py" %ARGS%
set "RC=%ERRORLEVEL%"

if /i not "%~1"=="--quiet" (
  echo.
  echo ---------------------------------------------
  echo  exit code = %RC%   ^(0 = ok, 1 = mail failed^)
  echo  log file  = signal\logs\
  echo ---------------------------------------------
  pause
)
exit /b %RC%
