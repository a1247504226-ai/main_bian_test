@echo off
chcp 65001 >nul
setlocal
REM ==================================================================
REM  SWITCH  --  one-click toggle for real-money order placement
REM  Config file: config.json  ->  _master_switch
REM  Portable: uses this folder.
REM  Usage: run_switch.bat [on|off|show]
REM  NOTE: no parenthesised if-blocks, CJK folder names break cmd there.
REM ==================================================================
cd /d "%~dp0"

set "PY="
if exist "C:\Users\hongji\.workbuddy-ai\binaries\python\versions\3.13.12\python.exe" set "PY=C:\Users\hongji\.workbuddy-ai\binaries\python\versions\3.13.12\python.exe"
if defined PY goto py_found
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if defined PY goto py_found
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PY goto py_found
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if defined PY goto py_found
if exist "C:\Python311\python.exe" set "PY=C:\Python311\python.exe"
if defined PY goto py_found
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto py_found
where py >nul 2>nul
if not errorlevel 1 set "PY=py"
if defined PY goto py_found

echo.
echo [ERROR] Python not found.
echo         Install Python 3.8+ from python.org and tick "Add Python to PATH".
echo         Or open this .bat in Notepad and set PY= to your python.exe path.
echo.
pause
exit /b 9009

:py_found

if not exist "config.json" goto no_config
if "%~1"=="" goto MENU
if /I "%~1"=="on" goto ON
if /I "%~1"=="off" goto OFF
if /I "%~1"=="show" goto SHOW
echo Usage: run_switch.bat [on^|off^|show]
exit /b 1

:no_config
echo.
echo [ERROR] config.json not found in this folder:
echo         %CD%
echo   Copy the WHOLE folder, do not hand-pick files.
echo.
pause
exit /b 2

:MENU
echo ============================================================
echo   BTC strategy master switch
echo     [1] Show current setting
echo     [2] OFF  - observe only  (no orders, safe)
echo     [3] ON   - LIVE real money (places real orders)
echo ============================================================
set "CH="
set /p CH=Choose 1/2/3: 
if "%CH%"=="1" goto SHOW
if "%CH%"=="2" goto OFF
if "%CH%"=="3" goto ON
echo Invalid choice.
exit /b 1

:SHOW
"%PY%" -c "import json,io;c=json.load(io.open('config.json',encoding='utf-8'));m=c['_master_switch'];t=c['trade'];print('mode =',m['mode']);print('i_understand_real_money =',m['i_understand_real_money']);print('trade.dry_run =',t['dry_run']);print('trade.profile =',t['profile']);print();print('REAL ORDERS:', 'ENABLED' if (m['mode']=='live' and m['i_understand_real_money'] and not t['dry_run']) else 'DISABLED (safe)')"
echo.
pause
exit /b 0

:OFF
"%PY%" -c "import json,io;p='config.json';c=json.load(io.open(p,encoding='utf-8'));c['_master_switch']['mode']='observe';c['_master_switch']['i_understand_real_money']=False;c['trade']['enabled']=False;c['trade']['dry_run']=True;json.dump(c,io.open(p,'w',encoding='utf-8'),ensure_ascii=False,indent=2);print('OK -^> OBSERVE MODE. Only email alerts, no orders.')"
echo.
pause
exit /b 0

:ON
echo.
echo ************************************************************
echo  WARNING: This enables REAL MONEY order placement on Binance.
echo  Orders will be submitted to the exchange automatically.
echo ************************************************************
echo.
set "OK="
set /p OK=Type YES (uppercase) to confirm: 
if not "%OK%"=="YES" goto cancelled
"%PY%" -c "import json,io;p='config.json';c=json.load(io.open(p,encoding='utf-8'));c['_master_switch']['mode']='live';c['_master_switch']['i_understand_real_money']=True;c['trade']['enabled']=True;c['trade']['dry_run']=False;json.dump(c,io.open(p,'w',encoding='utf-8'),ensure_ascii=False,indent=2);print('OK -^> LIVE MODE. Real orders ENABLED.')"
echo.
echo Next: run_sync_check.bat  then  run_trade.bat
echo.
pause
exit /b 0

:cancelled
echo Cancelled.
pause
exit /b 1
