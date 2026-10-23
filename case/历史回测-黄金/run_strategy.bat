@echo off
setlocal
cd /d "%~dp0"
if not exist "logs" mkdir "logs"
if not exist "output" mkdir "output"

rem Prefer the isolated venv python (has akshare installed), else fall back to PATH python
set "PY=C:\Users\hongji\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" "%~dp0gold_strategy.py" >> "%~dp0logs\run.log" 2>&1
exit /b %errorlevel%
