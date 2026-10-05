@echo off
REM ============================================================================
REM  MACD-X 每日信号检查 + 邮件推送
REM
REM  为什么定 08:01：币安日线在 UTC 00:00 收盘 = 北京时间 08:00，
REM  08:01 跑正好拿到刚收盘的那根日线，1 分钟足够 API 更新数据。
REM
REM  【重要】本文件必须保存为 ANSI/GBK 编码，不能用 UTF-8！
REM  cmd.exe 按 GBK 解析批处理文件，存成 UTF-8 会导致中文注释字节错位，
REM  把后续命令行全部冲乱（报"不是内部或外部命令"）。用记事本另存为
REM  "ANSI" 即可。
REM
REM  【日志编码】日志文件是 UTF-8（由 Python 写出），用记事本 / VS Code 打开。
REM  本 bat 自己写入日志的行刻意只用 ASCII，避免和 Python 的 UTF-8 输出混编码。
REM
REM  ==== 注册定时任务（管理员身份运行 CMD，执行一次即可）====
REM  schtasks /Create /TN "MACD-X每日信号" /TR "C:\Users\hongji\WorkBuddy AI\2026-09-29-16-53-45\run_signal.bat" /SC DAILY /ST 08:01 /F
REM
REM  查看：  schtasks /Query /TN "MACD-X每日信号" /V /FO LIST
REM  立即跑：schtasks /Run /TN "MACD-X每日信号"
REM  删除：  schtasks /Delete /TN "MACD-X每日信号" /F
REM
REM  注意：任务计划程序默认"仅在用户登录时运行"，且电脑关机/休眠时会跳过。
REM  要更可靠，注册后打开"任务计划程序"图形界面，在任务属性里勾选：
REM     [V] 不管用户是否登录都要运行
REM     [V] 如果错过计划则尽快启动
REM ============================================================================

chcp 936 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

REM ==== 监控标的与下单参数（要改就改这三行）====
set SYMBOLS=BTCUSDT ETHUSDT SOLUSDT
set CAPITAL=1000
set FRAC=0.7

REM ==== 找 Python：优先独立环境，其次系统安装路径，最后 PATH ====
set PY=
if exist "C:\Users\hongji\.workbuddy-ai\binaries\python\versions\3.13.12\python.exe" set PY=C:\Users\hongji\.workbuddy-ai\binaries\python\versions\3.13.12\python.exe
if not defined PY if exist "C:\Users\hongji\AppData\Local\Programs\Python\Python39\python.exe" set PY=C:\Users\hongji\AppData\Local\Programs\Python\Python39\python.exe
if not defined PY for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set PY=%%i
if not defined PY (
    if not exist logs mkdir logs
    echo %date% %time% [ERROR] python.exe not found, edit PY in run_signal.bat >> logs\macdx_run.log
    echo [ERROR] python.exe not found. Please edit the PY variable in this file.
    pause
    exit /b 1
)

REM ==== 日志按天分文件（用 PowerShell 取日期，%date% 在不同区域格式下会错位）====
if not exist logs mkdir logs
set TODAY=
for /f "delims=" %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd" 2^>nul') do set TODAY=%%i
if not defined TODAY set TODAY=%date:~0,4%%date:~5,2%%date:~8,2%
set LOG=logs\macdx_%TODAY%.log

REM ==== 以下写入日志的行只用 ASCII，避免和 Python 的 UTF-8 输出混编码 ====
echo. >> "%LOG%"
echo ============================================================ >> "%LOG%"
echo [%date% %time%] RUN START  python=%PY% >> "%LOG%"
echo [%date% %time%] SYMBOLS=%SYMBOLS%  CAPITAL=%CAPITAL%U  FRAC=%FRAC% >> "%LOG%"

REM ==== 正式执行 ====
"%PY%" macdx_strategy.py --symbols %SYMBOLS% --signal --capital %CAPITAL% --frac %FRAC% >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%

echo [%date% %time%] RUN END  exit_code=%RC% >> "%LOG%"
if not %RC%==0 echo [%date% %time%] WARN: non-zero exit, check this log >> "%LOG%"

REM ==== 清理 30 天前的日志 ====
forfiles /p logs /m macdx_*.log /d -30 /c "cmd /c del @path" >nul 2>&1

exit /b %RC%
