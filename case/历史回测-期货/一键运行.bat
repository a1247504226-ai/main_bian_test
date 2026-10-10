@echo off
chcp 936 >nul
title 期货策略 - 每日信号
cd /d "%~dp0"

echo ============================================================
echo   期货策略 - 每日信号检查
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
  echo [完成] 信号已检查完毕。
  echo        信号有变化 - 邮件已发到 1247504226@qq.com
  echo        信号没变化 - 不发邮件，这是正常的，不用管。
) else (
  echo [失败] 运行出错，错误码 %RC%
  echo        请把本窗口内容截图发给助手排查。
)
echo ------------------------------------------------------------
echo.
pause
