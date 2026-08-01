@echo off
chcp 65001 >nul
title Create desktop shortcuts
rem ---------------------------------------------------------------
rem  Run once - puts "start" and "stop" shortcuts on the desktop.
rem  ASCII-only on purpose, see vps_start.bat for why.
rem ---------------------------------------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make_shortcuts.ps1"
echo.
echo Press any key to close.
pause >nul
