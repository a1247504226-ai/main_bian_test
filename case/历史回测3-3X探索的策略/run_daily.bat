@echo off
chcp 65001 >nul
setlocal
REM ===== Same as run_all.bat (kept for Task Scheduler compatibility) =====
REM Portable: uses the folder this .bat lives in, no hardcoded paths.
REM NOTE: no parenthesised if-blocks anywhere, because CJK folder
REM       names break cmd's parser inside ( ) blocks.
cd /d "%~dp0"

set "PY="
if exist "C:\Users\hongji\.workbuddy-ai\binaries\python\versions\3.13.12\python.exe" set "PY=C:\Users\hongji\.workbuddy-ai\binaries\python\versions\3.13.12\python.exe"
if defined PY goto py_found
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if defined PY goto py_found
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PY goto py_found
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if defined PY goto py_found
if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if defined PY goto py_found
if exist "C:\Python313\python.exe" set "PY=C:\Python313\python.exe"
if defined PY goto py_found
if exist "C:\Python312\python.exe" set "PY=C:\Python312\python.exe"
if defined PY goto py_found
if exist "C:\Python311\python.exe" set "PY=C:\Python311\python.exe"
if defined PY goto py_found
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto py_found
where py >nul 2>nul
if not errorlevel 1 set "PY=py"
if defined PY goto py_found

echo.
echo [ERROR] Python not found.
echo         Install Python 3.8+ from python.org and tick "Add Python to PATH".
echo         Or open this .bat in Notepad and set PY= to your python.exe path.
echo.
pause
exit /b 9009

:py_found


if exist "run_all.py" goto have_script

echo.
echo [ERROR] run_all.py not found in this folder:
echo         %CD%
echo.
echo   You copied only the .bat files. The .py files are missing.
echo   Copy the WHOLE folder, do not hand-pick files.
echo.
echo   Double-click  check_files.bat  to see exactly what is missing.
echo.
pause
exit /b 2

:have_script
"%PY%" run_all.py %*
exit /b %errorlevel%
