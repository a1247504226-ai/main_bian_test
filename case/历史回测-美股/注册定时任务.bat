@echo off
REM ============================================================
REM  一键注册/更新 Windows 计划任务（每天 20:30 自动发信号邮件）
REM  双击运行即可；换电脑或挪了文件夹后，再双击一次就能重装。
REM ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo 正在注册 Windows 计划任务 US_Trend_Signal_Daily（每天 20:30 北京时间）...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0注册定时任务.ps1"

echo.
echo 完成。之后每天 20:30 会自动跑，无需手动点击。
echo 查看任务：schtasks /Query /TN "US_Trend_Signal_Daily"
echo.
pause
endlocal
