@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Not installed yet.
    echo Please double-click setup.bat first.
    pause
    exit /b 1
)

echo Starting Ticket Bot Web GUI ...
echo Browser will open at http://127.0.0.1:7860
echo Press Ctrl+C or close this window to stop.
echo.

".venv\Scripts\python.exe" run_webgui.py

pause
