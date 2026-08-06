@echo off
REM One-time AWS credential setup for the Tokyo (ap-northeast-1) instance.
REM Keys are typed into your own terminal and written straight to %USERPROFILE%\.aws
REM so they never pass through a chat window.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0aws_setup_credentials.ps1"
pause
