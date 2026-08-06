@echo off
REM Boot the Tokyo box (TicketPlus), open the SSH tunnel and the War-Room.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tokyo_start.ps1"
pause
