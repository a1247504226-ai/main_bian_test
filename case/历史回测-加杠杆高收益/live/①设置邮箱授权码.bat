@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   第一步：设置 163 邮箱授权码
echo ============================================================
echo.
echo   说明：这里要填的不是邮箱登录密码，而是 163 的
echo         「客户端授权码」（SMTP 专用密码）。
echo.
echo   获取方式：登录 mail.163.com ，进入 设置 ，再进
echo         POP3/SMTP/IMAP ，开启 SMTP 服务后即可获取授权码。
echo.
if defined STRATEGY_EMAIL_PASSWORD (
    echo   [提示] 当前环境变量已存在，重新设置将覆盖它。
    echo.
)
set /p CODE=请粘贴授权码，然后按回车：

if "%CODE%"=="" (
    echo.
    echo   [取消] 未输入任何内容。
    pause
    exit /b 1
)

setx STRATEGY_EMAIL_PASSWORD "%CODE%" >nul
if errorlevel 1 (
    echo   [错误] 设置失败，请尝试以管理员身份运行本脚本。
    pause
    exit /b 1
)

echo.
echo   [完成] 授权码已写入用户环境变量 STRATEGY_EMAIL_PASSWORD
echo.
echo   请关闭本窗口，重新双击 ②测试邮件.bat 验证。
echo   （环境变量只对新开的窗口生效，所以必须重开）
echo.
pause
