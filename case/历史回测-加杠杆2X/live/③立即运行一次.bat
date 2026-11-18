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

echo 正在拉取 BTC 行情并判定状态...
echo.
%PYEXE% live_monitor.py --once
echo.
echo 运行结束。状态已写入 state.json，日志见 monitor.log
echo.
echo 提示：计划任务每小时零 2 分自动运行一次，
echo       只有状态发生变化时才会发邮件。
echo.
pause
