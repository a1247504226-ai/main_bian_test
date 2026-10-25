@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PYEXE="
if exist ".venv\Scripts\python.exe" set "PYEXE=.venv\Scripts\python.exe"
if defined PYEXE goto :havepy
python -c "import sys" >nul 2>&1
if not errorlevel 1 set "PYEXE=python"
if defined PYEXE goto :havepy
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PYEXE=py -3"
if defined PYEXE goto :havepy
echo [错误] 没找到 Python，请先双击 install.bat 完成安装。
pause
exit /b 1

:havepy

echo ============================================================
echo   当前状态
echo ============================================================
echo.
%PYEXE% live_monitor.py --status
echo.
echo ============================================================
echo   计划任务状态
echo ============================================================
schtasks /Query /TN "BTC策略监测器" >nul 2>&1
if errorlevel 1 (
    echo   未注册（定时监测未开启）
) else (
    echo   已注册，每小时零 2 分运行一次
    schtasks /Query /TN "BTC策略监测器" /FO LIST | findstr /C:"Next Run Time" /C:"Last Run Time" /C:"Last Result"
)
echo.
pause
