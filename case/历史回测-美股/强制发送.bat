@echo off
REM ============================================================
REM  Force send one mail right now.
REM  Ignores trading-day / time-window checks.
REM  Extra args are forwarded, e.g.   强制发送.bat --dry-run
REM ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

set "PYEXE="
if exist "%~dp0signal\python_exe.txt" (
  for /f "usebackq delims=" %%i in ("%~dp0signal\python_exe.txt") do set "PYEXE=%%i"
)
if not defined PYEXE set "PYEXE=python"

if not exist "%~dp0signal\logs" mkdir "%~dp0signal\logs"

set "ARGS=%*"
set "ARGS=%ARGS:--quiet=%"

"%PYEXE%" "%~dp0signal\daily_signal.py" --force %ARGS%
set "RC=%ERRORLEVEL%"

echo.
echo ---------------------------------------------
echo  exit code = %RC%   ^(0 = ok, 1 = mail failed^)
echo ---------------------------------------------
pause
exit /b %RC%
