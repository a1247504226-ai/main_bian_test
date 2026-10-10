@echo off
title 期货策略 - 卸载定时任务
echo 正在删除计划任务「期货策略每日信号」...
echo.
schtasks /Delete /TN "期货策略每日信号" /F
echo.
if "%ERRORLEVEL%"=="0" (
  echo [完成] 定时任务已删除，以后不会再自动运行。
) else (
  echo [提示] 没找到该任务，或需要管理员权限。
)
echo.
pause
