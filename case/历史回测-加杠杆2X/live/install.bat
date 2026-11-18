@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================================
echo   BTC 趋势策略监测器 - 一键安装 (Windows)
echo ============================================================
echo.

REM ---------- 1. 找一个真正能用的 Python ----------
set PY=
call :try "python"
if not defined PY call :try "py -3.13"
if not defined PY call :try "py -3.12"
if not defined PY call :try "py -3.11"
if not defined PY call :try "py -3.10"
if not defined PY call :try "py -3"
if not defined PY call :try "py"
if not defined PY goto :nopython
echo.
goto :havepython

:try
REM %~1 = 待测试的命令；要求：能出版本号 + 标准库齐全
%~1 --version >nul 2>&1
if errorlevel 1 exit /b 1
%~1 -c "import urllib.request,smtplib,ssl,json" >nul 2>&1
if errorlevel 1 exit /b 1
set "PY=%~1"
echo [1/5] 选定 Python: %~1
%~1 --version
exit /b 0

:nopython
echo [错误] 没找到可用的 Python。
echo        请从 https://www.python.org/downloads/ 安装，
echo        安装时勾选 "Add Python to PATH"，然后重跑本脚本。
echo.
pause
exit /b 1

:havepython
REM ---------- 2. 建虚拟环境（可自愈：拷到别的电脑后旧 .venv 会失效，自动重建） ----------
echo [2/5] 准备虚拟环境 .venv ...
set "VENV_OK=0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import urllib.request,smtplib,ssl,json" >nul 2>&1
    if not errorlevel 1 set "VENV_OK=1"
)
if "%VENV_OK%"=="1" (
    echo      .venv 已可用，跳过
) else (
    if exist ".venv" (
        echo      检测到 .venv 不可用（多半来自别的电脑），正在重建...
        rmdir /s /q ".venv"
    )
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败，请换一个 Python 版本重试。
        pause
        exit /b 1
    )
    echo      虚拟环境就绪
)
echo.

REM ---------- 3. 检查依赖（本策略只用标准库） ----------
echo [3/5] 检查依赖 ...
".venv\Scripts\python.exe" -c "import urllib.request, smtplib, ssl, json" >nul 2>&1
if errorlevel 1 (
    echo [警告] 标准库异常，请检查 Python 安装是否完整。
) else (
    echo      只需标准库，无需安装第三方包
)
echo.

REM ---------- 4. 配置文件 ----------
echo [4/5] 检查配置文件 ...
if not exist "config.json" (
    echo [错误] 缺少 config.json
    pause
    exit /b 1
)
echo      config.json 就绪
echo.

REM ---------- 5. 注册计划任务（每小时零 2 分） ----------
echo [5/5] 注册 Windows 计划任务（每小时 02 分运行一次，失败自动重试 3 次）...
call "%~dp0_reg_task.bat"
if errorlevel 1 (
    echo [警告] 注册计划任务失败。
    echo        常见原因是权限不足：请右键本脚本，选择"以管理员身份运行"，
    echo        或改用常驻模式：.venv\Scripts\python.exe live_monitor.py --loop
) else (
    echo      计划任务已注册：BTC策略监测器
)
echo.

echo ============================================================
echo   安装完成
echo ============================================================
echo.
echo   下一步：设置 163 邮箱授权码（客户端授权码，不是登录密码）
echo.
echo     setx STRATEGY_EMAIL_PASSWORD "你的163授权码"
echo.
echo   设置后必须重新打开命令行窗口，然后依次运行：
echo.
echo     .venv\Scripts\python.exe live_monitor.py --status
echo     .venv\Scripts\python.exe live_monitor.py --test-email
echo     .venv\Scripts\python.exe live_monitor.py --once
echo.
echo   查看日志：type monitor.log
echo   停止定时监测：schtasks /Delete /TN "BTC策略监测器" /F
echo.
pause
exit /b 0
