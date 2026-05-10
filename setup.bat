@echo off
setlocal
cd /d "%~dp0"

echo.
echo ============================================
echo   Ticket Bot - First-time Setup
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 goto NO_PYTHON

echo [INFO] Found Python:
python --version
echo.

python -c "import sys; raise SystemExit(0 if sys.version_info[:2] in ((3,10),(3,11),(3,12)) else 1)"
if errorlevel 1 goto BAD_VERSION

echo [OK] Python version is supported.
echo.

if exist ".venv\Scripts\python.exe" goto VENV_OK
echo [STEP] Creating virtual env at .venv\ ...
python -m venv .venv
if errorlevel 1 goto VENV_FAIL

:VENV_OK
echo [STEP] Upgrading pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto PIP_UPGRADE_FAIL

echo.
echo [STEP] Installing packages. This takes 2 to 5 minutes...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto PIP_INSTALL_FAIL

echo.
echo ============================================
echo   Setup complete!
echo   Now double-click start.bat to launch.
echo ============================================
pause
exit /b 0


:NO_PYTHON
echo [ERROR] Python not found on PATH.
echo.
echo Please install Python 3.10, 3.11, or 3.12 from:
echo   https://www.python.org/downloads/
echo During install, MUST check "Add Python to PATH"
echo.
pause
exit /b 1

:BAD_VERSION
echo.
echo [ERROR] Wrong Python version. Need 3.10, 3.11, or 3.12.
echo curl_cffi has no wheel for 3.13+ yet.
echo Currently installed:
python --version
echo.
pause
exit /b 1

:VENV_FAIL
echo [ERROR] venv creation failed.
pause
exit /b 1

:PIP_UPGRADE_FAIL
echo [ERROR] pip upgrade failed.
pause
exit /b 1

:PIP_INSTALL_FAIL
echo.
echo [ERROR] Package install failed.
echo Screenshot the message above and ask the person who gave you this.
pause
exit /b 1
