@echo off
chcp 65001 >nul
title Tixcraft VPS - START
rem ---------------------------------------------------------------
rem  Double-click launcher for vps_start.ps1
rem
rem  -ExecutionPolicy Bypass: avoids the "script is not digitally
rem  signed" block without changing machine-wide policy.
rem  Keep this file ASCII-only - cmd reads it with the console
rem  codepage and any CJK text here would come out garbled.
rem  All human-readable output lives in the .ps1 instead.
rem ---------------------------------------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0vps_start.ps1"
echo.
echo Press any key to close this window. The SSH tunnel keeps running.
pause >nul
