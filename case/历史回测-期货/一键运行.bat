@echo off
chcp 936 >nul
title 期货策略 - 每日信号
cd /d "%~dp0"

echo ============================================================
echo   期货策略 - 每日信号推送
echo   运行时间：%date% %time%
echo ============================================================
echo.

set "PY=C:\Users\hongji\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" strategy_top2.py notify
set RC=%ERRORLEVEL%

echo.
echo ------------------------------------------------------------
if "%RC%"=="0" (
  echo [完成] 邮件已发到 1247504226@qq.com
  echo        有仓位变动 - 标题会带「有变更」重点提醒
  echo        没有变动   - 标题报当前浮盈/浮亏
  echo        15 点前跑算早盘场次，15 点后算晚盘场次，两场各自去重。
  echo        若提示"这一场已经推过了"，想再发就双击「强制发送.bat」。
) else (
  echo [失败] 运行出错，错误码 %RC%
  echo        请把本窗口内容截图发给助手排查。
)
echo ------------------------------------------------------------
echo.
pause
