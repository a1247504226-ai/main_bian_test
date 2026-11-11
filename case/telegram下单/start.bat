@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 币安 TG 跟单机器人

echo ==================================================
echo    币安 TG 跟单机器人
echo ==================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 找不到 python，请先安装 Python 3.9+ 并加入 PATH
    pause
    exit /b 1
)

if not exist ".env.bot" (
    echo [错误] 缺少 .env.bot
    echo        请复制 .env.bot.example 为 .env.bot 并填好密钥
    pause
    exit /b 1
)

python -c "import requests, yaml, dotenv" >nul 2>nul
if errorlevel 1 (
    echo [提示] 首次运行，正在安装依赖...
    python -m pip install -q -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败
        pause
        exit /b 1
    )
)

echo 选择运行模式：
echo.
echo   [1] 测试网 + 只打印      最安全，验证信号解析
echo   [2] 测试网 + 真实下单    虚拟资金，可放心测
echo   [3] 回放历史消息         离线验证解析和下单逻辑
echo   [4] 检查配置             看环境/密钥/余额
echo   [5] 主网 + 真实下单      真实资金，谨慎
echo.
set "choice="
set /p "choice=请输入序号 [1]: "
if "%choice%"=="" set "choice=1"

if "%choice%"=="1" goto MODE1
if "%choice%"=="2" goto MODE2
if "%choice%"=="3" goto MODE3
if "%choice%"=="4" goto MODE4
if "%choice%"=="5" goto MODE5
echo 无效选择
pause
exit /b 1

:MODE1
echo.
echo === 测试网 + 只打印 ===
python trader.py
goto END

:MODE2
echo.
echo === 测试网 + 真实下单（虚拟资金）===
python trader.py --live
goto END

:MODE3
echo.
set "f="
set /p "f=请输入历史消息文件路径（可直接回车用默认）: "
if "%f%"=="" set "f=D:\Down\cyclone\telegram_history_10000.txt"
if not exist "%f%" (
    echo [错误] 文件不存在: %f%
    goto END
)
python trader.py --replay "%f%" --limit 50
goto END

:MODE4
echo.
python main.py check
goto END

:MODE5
echo.
echo ##################################################
echo   警告：即将用【真实资金】在币安主网下单
echo   请确认 .env.bot 里已配置 BINANCE_MAINNET_API_KEY
echo ##################################################
echo.
set "c="
set /p "c=输入 YES 继续: "
if /i not "%c%"=="YES" (
    echo 已取消
    goto END
)
python trader.py --mainnet --live
goto END

:END
echo.
echo ==================================================
echo 程序已退出
echo ==================================================
pause
