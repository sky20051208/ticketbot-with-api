@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto NO_VENV

echo.
echo ============================================
echo   Ticket Bot - Create Login Profile
echo ============================================
echo.
echo This saves the login state for ONE account on ONE platform.
echo Run it once for each account (and each platform) you want to use.
echo.

set "ACCNAME="
set /p ACCNAME="Enter a name for this account [English letters, no spaces]: "

if "%ACCNAME%"=="" goto NO_NAME

echo.
echo Which platform?
echo   1 = Tixcraft
echo   2 = KKTIX
echo   3 = TicketPlus
echo.
set "PLATCHOICE="
set /p PLATCHOICE="Enter 1, 2, or 3 [default 1]: "

if "%PLATCHOICE%"=="2" goto PLAT_KKTIX
if "%PLATCHOICE%"=="3" goto PLAT_TICKETPLUS
goto PLAT_TIXCRAFT

:PLAT_KKTIX
set "PLATFORM=kktix"
goto PLAT_DONE

:PLAT_TICKETPLUS
set "PLATFORM=ticketplus"
goto PLAT_DONE

:PLAT_TIXCRAFT
set "PLATFORM=tixcraft"
goto PLAT_DONE

:PLAT_DONE

echo.
echo [STEP] Opening Chrome for profile: %ACCNAME% (%PLATFORM%)
echo [STEP] Log in in that window, then close it and press Enter here.
echo.

".venv\Scripts\python.exe" create_profile.py --name "%ACCNAME%" --platform %PLATFORM%

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
