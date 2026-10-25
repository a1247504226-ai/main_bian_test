@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   停止定时监测
echo ============================================================
echo.
echo   即将删除计划任务「BTC策略监测器」。
echo   这只会停掉自动运行，不会删除任何文件，
echo   之后你仍可双击 install.bat 或 ③立即运行一次.bat 手动跑。
echo.
set /p OK=确认停止？输入 y 后按回车：

if /i not "%OK%"=="y" (
    echo.
    echo   已取消。
    pause
    exit /b 0
)

schtasks /Delete /TN "BTC策略监测器" /F
if errorlevel 1 (
    echo.
    echo   [失败] 删除未成功，可能任务不存在或权限不足。
) else (
    echo.
    echo   [完成] 定时监测已停止。
)
echo.
pause
