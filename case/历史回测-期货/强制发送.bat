@echo off
chcp 936 >nul
title 期货策略 - 强制发送
cd /d "%~dp0"

echo ============================================================
echo   强制发送一封当日邮件（数据没更新也发）
echo   运行时间：%date% %time%
echo ============================================================
echo.

set "PY=C:\Users\hongji\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" strategy_top2.py notify daily
set RC=%ERRORLEVEL%

echo.
echo ------------------------------------------------------------
if "%RC%"=="0" (
  echo [完成] 邮件已发出。
) else (
  echo [失败] 运行出错，错误码 %RC%
)
echo ------------------------------------------------------------
echo.
pause
