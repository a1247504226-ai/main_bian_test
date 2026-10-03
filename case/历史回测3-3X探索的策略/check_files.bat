@echo off
chcp 65001 >nul
setlocal
REM ===================================================================
REM  File completeness self-check.  Double-click this file.
REM  It lists every required file and tells you what is missing.
REM  NOTE: no parenthesised if-blocks, CJK folder names break cmd there.
REM ===================================================================
cd /d "%~dp0"

echo.
echo ==========================================================
echo   BTC live package - file check
echo   Folder: %CD%
echo ==========================================================
echo.

set "MISSING=0"
set "OKCOUNT=0"

for %%F in (run_all.py common.py alert.py binance.py status.py sync_check.py trade.py verify_live.py strategy_a.py strategy_b.py strategy_c.py config.json run_all.bat run_status.bat run_switch.bat run_trade.bat run_sync_check.bat run_alert.bat run_daily.bat) do call :chk "%%F"

echo.
echo ----------------------------------------------------------
if %MISSING%==0 goto all_ok
echo   RESULT: %MISSING% FILE(S) MISSING ^(%OKCOUNT% found^).
echo.
echo   You must copy the WHOLE folder, not selected files.
echo.
echo   Right way: copy the entire package folder as a whole,
echo   paste it here, and let Windows merge. Do NOT hand-pick files.
goto summary_end

:all_ok
echo   RESULT: ALL %OKCOUNT% FILES PRESENT - you are good to go.
echo   Next: double-click  run_all.bat  to see today's signals.

:summary_end
echo ----------------------------------------------------------
echo.

if not exist "logs" mkdir "logs"

echo Press any key to close . . .
pause >nul
exit /b %MISSING%

:chk
if exist %1 goto chk_ok
echo   [MISSING]  %~1
set /a MISSING+=1
goto :eof

:chk_ok
echo   [ OK ]  %~1
set /a OKCOUNT+=1
goto :eof
