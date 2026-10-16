@echo off
title 期货策略 - 安装定时任务
cd /d "%~dp0"

echo ============================================================
echo   安装 Windows 计划任务（两个）
echo     早盘场次：每周一至周五 08:00  = 日盘 09:00 开盘前 1 小时
echo     晚盘场次：每周一至周五 20:00  = 夜盘 21:00 开盘前 1 小时
echo   运行结果写入：logs\auto.log
echo ============================================================
echo.

set "DIR=%~dp0"

schtasks /Create /TN "期货策略信号-早盘" /TR "\"%DIR%task_run.bat\" am" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 08:00 /F
set RC1=%ERRORLEVEL%
schtasks /Create /TN "期货策略信号-晚盘" /TR "\"%DIR%task_run.bat\" pm" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 20:00 /F
set RC2=%ERRORLEVEL%

echo.
if "%RC1%%RC2%"=="00" (
  echo [成功] 两个定时任务都已安装。
  echo        以后每个工作日 08:00 和 20:00 各推一封，不需要你管。
  echo        两封内容不完全一样：早盘那封会用夜盘最新价算浮盈。
  echo        想立刻手动跑一次，双击「一键运行.bat」即可。
) else (
  echo [失败] 至少有一个没装上。
  echo        请关掉本窗口，右键本文件 - 以管理员身份运行，再试一次。
)
echo.
echo 当前已注册的期货任务：
schtasks /Query /TN "期货策略信号-早盘" 2>nul
schtasks /Query /TN "期货策略信号-晚盘" 2>nul
echo.
pause
