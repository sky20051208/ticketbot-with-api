@echo off
rem ---------------------------------------------------------------
rem  Tixcraft US-East VPS control panel.
rem  -WindowStyle Hidden: the panel is a GUI, no console needed.
rem  ASCII-only on purpose, see vps_start.bat for why.
rem ---------------------------------------------------------------
start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0vps_panel.ps1"
