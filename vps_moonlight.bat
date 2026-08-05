@echo off
REM Connect to the VPS desktop over H.264 (Sunshine + Moonlight).
REM Measured 1440x900 @ 60fps: 3.9 Mbps, 23%% of the Taiwan-Ashburn path capacity.
REM The VNC route (vps_vnc.bat) tops out around 16fps on the same link.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0vps_moonlight.ps1"
