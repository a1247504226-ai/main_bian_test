@echo off
chcp 65001 >nul
REM 注册计划任务：每小时零 2 分运行 run_with_retry.bat（失败自动重试 3 次）
setlocal
cd /d "%~dp0"
schtasks /Create /TN "BTC策略监测器" /SC HOURLY /MO 1 /ST 00:02 /F /TR "%~dp0run_with_retry.bat" /RL LIMITED
exit /b %errorlevel%
