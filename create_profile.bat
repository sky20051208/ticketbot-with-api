@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto NO_VENV

echo.
echo ============================================
echo   Ticket Bot - Create Login Profile
echo ============================================
echo.
echo This saves the login state for ONE Tixcraft account.
echo Run it once for each account you want to use.
echo.

set "ACCNAME="
set /p ACCNAME="Enter a name for this account [English letters, no spaces]: "

if "%ACCNAME%"=="" goto NO_NAME

echo.
echo [STEP] Opening Chrome for profile: %ACCNAME%
echo [STEP] Log in to Tixcraft in that window, then close it and press Enter here.
echo.

".venv\Scripts\python.exe" create_profile.py --name "%ACCNAME%"

echo.
echo Done. The profile "%ACCNAME%" is now available in the Web GUI dropdown.
pause
exit /b 0


:NO_VENV
echo [ERROR] Not installed yet.
echo Please double-click setup.bat first.
pause
exit /b 1

:NO_NAME
echo.
echo [ERROR] No name entered. Run create_profile.bat again and type a name.
pause
exit /b 1
