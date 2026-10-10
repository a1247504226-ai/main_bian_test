@echo off
rem Silent runner used by the Windows scheduled task. ASCII only on purpose.
rem PYTHONIOENCODING=gbk keeps the log readable at code page 936 (no mixed encoding).
chcp 936 >nul
cd /d "%~dp0"
if not exist "logs" mkdir "logs"
set "PY=C:\Users\hongji\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONIOENCODING=gbk:replace"
echo ==== %date% %time% ==== >> "logs\auto.log"
"%PY%" strategy_top2.py notify >> "logs\auto.log" 2>&1
