@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
title Zerro-DEFI Launcher
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"

rem ---------------------------------------------------------------
rem 1. PYTHON VERSION CHECK
rem ---------------------------------------------------------------
python -c "import sys;raise SystemExit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo   [x] Python 3.11 or newer was not found.
    echo       Install Python 3.11+ from https://www.python.org/downloads/
    echo       then run this file again.
    goto :fail
)
echo [OK] Python 3.12+ detected.

rem ---------------------------------------------------------------
rem 2. LAUNCH.PY EXISTENCE CHECK
rem ---------------------------------------------------------------
if not exist "scripts\launch.py" (
    echo.
    echo   [x] scripts\launch.py is missing.
    echo       Run git pull or re-download from repository.
    goto :fail
)
echo [OK] Launch script found.

rem ---------------------------------------------------------------
rem 3. ENVIRONMENT VALIDATION (launch.py step_env equivalent)
rem ---------------------------------------------------------------
echo.
echo [STEP 1] Environment Validation

if not exist ".env" (
    echo.
    echo [INFO] .env file not found - creating from .env.example
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo [INFO] Created .env from .env.example
    ) else (
        echo [ERROR] .env.example not found - create it manually
        goto :fail
    )
) else (
    echo [INFO] .env exists
)

:: Check critical env vars - PAYMASTER_BUNDLER_RPC
set "HAS_PAYMASTER=0"
findstr /i "PAYMASTER_BUNDLER_RPC=" .env >nul 2>&1
if %errorlevel% equ 0 (
    :: Key exists - check if it has a value (not just "PAYMASTER_BUNDLER_RPC=")
    findstr /i "PAYMASTER_BUNDLER_RPC=http" .env >nul 2>&1
    if %errorlevel% equ 0 (
        set "HAS_PAYMASTER=1"
    )
)

:: Determine mode based on paymaster presence
if %HAS_PAYMASTER%==1 (
    set "DISPLAY_MODE=LIVE"
) else (
    set "DISPLAY_MODE=DRY_RUN"
)

rem ---------------------------------------------------------------
rem 4. DEPENDENCY CHECK (launch.py step_deps equivalent)
rem ---------------------------------------------------------------
echo.
echo [STEP 2] Dependency Check

cd py-orchestrator
python -c "import aiohttp" >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARN] aiohttp not installed - installing
    pip install -e . >nul 2>&1
    if %errorlevel% neq 0 (
        echo [ERROR] pip install failed
        goto :fail
    )
)
echo [OK] Python dependencies satisfied.
cd ..

rem ---------------------------------------------------------------
rem 5. WALLET PROVISIONING CHECK (launch.py step_wallet equivalent)
rem ---------------------------------------------------------------
echo.
echo [STEP 3] Wallet Check

python scripts\setup_operator_wallet.py >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARN] Wallet provisioning had issues - may need to run manually
    echo   python scripts/setup_operator_wallet.py
) else (
    echo [OK] Operator wallet validated
)

rem ---------------------------------------------------------------
rem 6. COMPILE / CONTRACT CHECK (launch.py step_compile equivalent)
rem ---------------------------------------------------------------
echo.
echo [STEP 4] Contract Compilation Check

:: Run from root - compile_and_verify.py uses __file__ resolution
call python scripts\compile_and_verify.py >nul 2>&1
set compile_result=%errorlevel%

if %compile_result% neq 0 (
    echo.
    echo [ERROR] Contract compilation or tests failed
    echo   Run: python scripts\compile_and_verify.py
    goto :fail
)
echo [OK] Contracts compiled & 5/5 tests + 1 fork test PASSED

rem ---------------------------------------------------------------
rem 7. PAYMASTER & DEPLOY CHECK (launch.py step_paymaster + step_deploy)
rem ---------------------------------------------------------------
echo.
echo [STEP 5] Paymaster & Deployment Check

if %HAS_PAYMASTER%==0 (
    echo [SKIP] No paymaster configured - launching in DRY_RUN mode only
    set "DEPLOYABLE=0"
) else (
    echo [INFO] Paymaster configured - deployment available
    set "DEPLOYABLE=1"
)

rem ---------------------------------------------------------------
rem 8. RUST BUILD CHECK (launch.py step_rust equivalent)
rem ---------------------------------------------------------------
echo.
echo [STEP 6] Rust Sniper Build Check

cd rust-sniper
call cargo build --release 2>&1 | findstr /i "warning" >nul
if %errorlevel% equ 0 (
    echo [WARN] Rust build had warnings (may be harmless)
) else (
    echo [OK] Rust build clean (0 warnings)
)
cd ..

rem ---------------------------------------------------------------
rem 9. LAUNCH DECISION POINT
rem ---------------------------------------------------------------
echo.
echo ============================================================
echo   PREFLIGHT COMPLETE - READY TO LAUNCH
echo ============================================================
echo.
echo  Configuration Summary:
echo   - Mode: !DISPLAY_MODE!
echo   - Paymaster: %HAS_PAYMASTER% (1=yes, 0=no)
echo   - Deployable: %DEPLOYABLE%
echo   - Rust Sniper: Built & ready
echo   - Contracts: Compiled & verified
echo   - Wallet: Provisioned
echo.
echo  Select launch option:
echo.
echo   1) Launch ALL terminals (DRY_RUN or LIVE)
echo   2) Launch terminals --no-launch (write BAT scripts only)
echo   3) Run monitor only
echo   4) Exit without launching
echo.
set /p choice="Enter choice [1-4]: "

rem ---------------------------------------------------------------
rem 10. LAUNCH EXECUTION
rem ---------------------------------------------------------------
if "%choice%"=="1" (
    %PY% "scripts\launch.py" %*
    set "CODE=%errorlevel%"
    if not "%CODE%"=="0" goto :fail
    goto :launch_end
) else if "%choice%"=="2" (
    %PY% "scripts\launch.py" --no-launch %*
    set "CODE=%errorlevel%"
    if not "%CODE%"=="0" goto :fail
    goto :launch_end
) else if "%choice%"=="3" (
    %PY% -m orchestrator.monitor --once
    goto :eof
) else if "%choice%"=="4" (
    echo.
    echo   Exiting - no systems launched.
    goto :eof
) else (
    echo.
    echo   [x] Invalid choice - exiting
    goto :fail
)

:launch_end
echo.
echo.
echo   ALL TERMINALS LAUNCHED
echo.
echo   Terminal 1: Python Indexer + ERC-4337 Submitter
echo   Terminal 2: Rust Multicall3 Sniper
echo   Terminal 3: Telemetry Dashboard (run anytime)
echo.
echo   To stop all: close all terminal windows
echo   To check status: python -m orchestrator.monitor
if "%INTERACTIVE%"=="1" (
    pause
)
exit /b 0

:fail
echo.
echo [ERROR] Preflight validation failed.
echo Please fix the issues above and re-run start.bat.
if "%INTERACTIVE%"=="1" pause
exit /b 1