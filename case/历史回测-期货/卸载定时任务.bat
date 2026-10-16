@echo off
title 期货策略 - 卸载定时任务
echo 正在删除两个计划任务...
echo.
schtasks /Delete /TN "期货策略信号-早盘" /F
schtasks /Delete /TN "期货策略信号-晚盘" /F
echo.
echo [完成] 如果上面显示"成功"，就说明已删除，以后不会再自动运行。
echo        如果显示"找不到"，说明那个任务本来就没装。
echo.
pause
