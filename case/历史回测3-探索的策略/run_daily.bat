@echo off
chcp 65001 >nul
setlocal
set "PY=C:\Users\hongji\.workbuddy-ai\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"
set "DIR=C:\Users\hongji\WorkBuddy AI\2026-09-29-21-48-51\btc_lev\live"
cd /d "%DIR%"
"%PY%" run_all.py
exit /b %errorlevel%
