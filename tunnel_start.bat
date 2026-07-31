@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   Remote Login - Cloudflare Quick Tunnel
echo ============================================
echo.
echo This exposes the local webgui [127.0.0.1:7860] as a temporary https URL.
echo Make sure the webgui is already running: python run_webgui.py
echo.
echo Below it will print a URL like  https://xxxx.trycloudflare.com
echo Copy that URL into the Remote-Login admin page field "public URL".
echo Closing this window kills the tunnel and the URL stops working.
echo.

where cloudflared >nul 2>nul
if errorlevel 1 goto NO_CF

cloudflared tunnel --url http://127.0.0.1:7860
goto END

:NO_CF
echo [ERROR] cloudflared not found on PATH.
echo Install it first:  winget install --id Cloudflare.cloudflared
pause

:END
