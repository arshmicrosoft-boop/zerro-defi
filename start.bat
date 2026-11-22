@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
title Zerro-DEFI Launcher
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"

rem Detect a double-click launch so the window stays open at the end.
set "INTERACTIVE=0"
echo %cmdcmdline% | findstr /i /c:"%~nx0" >nul 2>&1 && set "INTERACTIVE=1"

set "PY="
python -c "import sys;raise SystemExit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1 && set "PY=python"
if not defined PY (
    py -3 -c "import sys;raise SystemExit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
    echo.
    echo   [x] Python 3.11 or newer was not found.
    echo       Install it from https://www.python.org/downloads/
    echo       then run this file again.
    goto :fail
)

if not exist "scripts\launch.py" (
    echo.
    echo   [x] scripts\launch.py is missing.
    echo       Run start.bat from the repository root.
    goto :fail
)

%PY% "scripts\launch.py" %*
set "CODE=%errorlevel%"
if not "%CODE%"=="0" goto :fail

if "%INTERACTIVE%"=="1" (
    echo.
    pause
)
exit /b 0

:fail
echo.
if "%INTERACTIVE%"=="1" pause
exit /b 1
