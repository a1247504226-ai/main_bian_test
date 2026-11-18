@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 选择 Python：优先 .venv，缺失则系统 python / py -3（本策略纯标准库）
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
echo 正在发送测试邮件到 config.json 里配置的收件人...
echo.
%PYEXE% live_monitor.py --test-email
echo.
if errorlevel 1 (
    echo [失败] 发送未成功。请检查：
    echo   1. config.json 里 email.password 是否为 163 客户端授权码（不是登录密码）
    echo   2. 163 邮箱是否已开启 SMTP 服务
    echo   3. 网络是否正常
) else (
    echo [成功] 请去收件箱查收。若没收到，请看垃圾箱。
)
echo.
pause
