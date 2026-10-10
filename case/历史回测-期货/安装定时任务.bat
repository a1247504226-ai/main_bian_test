@echo off
title 期货策略 - 安装定时任务
cd /d "%~dp0"

echo ============================================================
echo   安装 Windows 计划任务：期货策略每日信号
echo   触发时间：每周一至周五 16:00（收盘后自动出信号）
echo   运行结果写入：logs\auto.log
echo ============================================================
echo.

set "DIR=%~dp0"
schtasks /Create /TN "期货策略每日信号" /TR "\"%DIR%task_run.bat\"" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 16:00 /F

echo.
if "%ERRORLEVEL%"=="0" (
  echo [成功] 定时任务已安装。
  echo        以后每个工作日 16:00 自动运行，不需要你管。
  echo        想立刻手动跑一次，双击「一键运行.bat」即可。
) else (
  echo [失败] 安装失败。
  echo        请关掉本窗口，右键本文件 - 以管理员身份运行，再试一次。
)
echo.
echo 当前已注册的期货任务：
schtasks /Query /TN "期货策略每日信号" 2>nul
echo.
pause
