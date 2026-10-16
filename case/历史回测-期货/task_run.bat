@echo off
rem Silent runner used by the Windows scheduled tasks. ASCII only on purpose.
rem Passes its argument (am / pm) through to notify so the two slots dedupe separately.
rem PYTHONIOENCODING=gbk keeps the log readable at code page 936 (no mixed encoding).
chcp 936 >nul
cd /d "%~dp0"
if not exist "logs" mkdir "logs"
set "PY=C:\Users\hongji\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONIOENCODING=gbk:replace"
echo ==== %date% %time% [slot=%*] ==== >> "logs\auto.log"
"%PY%" strategy_top2.py notify %* >> "logs\auto.log" 2>&1
