@echo off
REM Shut down the Tokyo box and clean up the SSH tunnel.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tokyo_stop.ps1"
pause
