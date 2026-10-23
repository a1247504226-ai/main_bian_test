@echo off
setlocal
set "TASK=GoldTrendStrategy"

echo ============================================================
echo  Uninstall scheduled task: %TASK%
echo ============================================================

schtasks /Delete /TN "%TASK%" /F
if errorlevel 1 (
  echo [WARN] Task not found, or need administrator permission.
) else (
  echo [OK] Task "%TASK%" removed.
)

echo.
pause
endlocal
