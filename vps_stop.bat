@echo off
chcp 65001 >nul
title Tixcraft VPS - STOP
rem ---------------------------------------------------------------
rem  Double-click launcher for vps_stop.ps1 - closes the tunnel and
rem  powers the instance off. Forgetting this is the difference
rem  between about 3 and about 76 US dollars a month.
rem  ASCII-only on purpose, see vps_start.bat for why.
rem ---------------------------------------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0vps_stop.ps1"
echo.
echo Press any key to close.
pause >nul
