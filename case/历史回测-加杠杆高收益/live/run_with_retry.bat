@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

rem 选择 Python：优先 .venv，缺失则回退系统 python / py（本策略纯标准库，无需第三方包）
set "PYEXE="
if exist ".venv\Scripts\python.exe" set "PYEXE=.venv\Scripts\python.exe"
if defined PYEXE goto :havepy
python -c "import sys" >nul 2>&1
if not errorlevel 1 set "PYEXE=python"
if defined PYEXE goto :havepy
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PYEXE=py -3"
if defined PYEXE goto :havepy
echo [%TIME%] 未找到 Python（.venv 或系统），请先运行 install.bat >> monitor.log
exit /b 1

:havepy
set "MAXTRY=3"
set "N=0"

:retry
set /a N+=1
%PYEXE% live_monitor.py --once
if errorlevel 1 goto failed
if %N% GTR 1 echo [%TIME%] 第 %N% 次尝试后成功
exit /b 0

:failed
if %N% LSS %MAXTRY% (
    echo [%TIME%] 第 %N% 次运行失败，30 秒后重试...
    rem delay 30s via ping (timeout may be unavailable)
    ping -n 31 127.0.0.1 >nul
    goto retry
)
echo [%TIME%] 已重试 %MAXTRY% 次仍失败，放弃本轮。详见 monitor.log
exit /b 1
