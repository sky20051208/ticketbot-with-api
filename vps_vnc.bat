@echo off
REM Open the VPS remote desktop with TigerVNC (native client, not the browser).
REM
REM Why native instead of noVNC: noVNC repaints a canvas from JavaScript, which
REM feels sluggish over the ~200ms Taiwan-Ashburn round trip. TigerVNC does Tight
REM encoding + JPEG natively and is noticeably smoother on the same link.
REM
REM Needs the SSH tunnel from vps_start.bat to be up (it forwards local 5900).
REM Install once with:  winget install --id TigerVNC.TigerVNC -e

setlocal
set VIEWER=C:\Program Files\TigerVNC\vncviewer.exe
if not exist "%VIEWER%" set VIEWER=C:\Program Files (x86)\TigerVNC\vncviewer.exe
if not exist "%VIEWER%" goto noviewer

REM Is the tunnel up? Without it the viewer just shows a vague connection error.
powershell -NoProfile -Command "$c=New-Object Net.Sockets.TcpClient; try{$c.Connect('127.0.0.1',5900); exit 0}catch{exit 1}"
if errorlevel 1 goto notunnel

REM Measured Taiwan -> Ashburn: ~2.1 MB/s total, and that is the whole ceiling
REM (4 parallel streams gained almost nothing). So smoothness is decided entirely
REM by bytes-per-frame, not by the VM - its CPU never went above 8% during any of this.
REM
REM AutoSelect=0 keeps it from renegotiating settings mid-session on a jittery link.
REM CompressLevel 6 trades VPS CPU (there is spare) for fewer bytes on the wire.
REM QualityLevel 4 measured 146KB/frame vs 352KB at 6. Going below 4 is pointless:
REM Q2 measured 153KB - by then the cost is the lossless text regions, not the JPEG,
REM so you only lose sharpness and save nothing.
start "" "%VIEWER%" -AutoSelect=0 -PreferredEncoding=Tight -CompressLevel=6 -QualityLevel=4 -Shared=1 -DotWhenNoCursor=1 localhost::5900
goto :eof

:noviewer
echo.
echo   TigerVNC is not installed. Run this once:
echo.
echo       winget install --id TigerVNC.TigerVNC -e
echo.
pause
goto :eof

:notunnel
echo.
echo   Local port 5900 is not listening - the SSH tunnel is down.
echo   Press the start button on the panel first (vps_start.bat).
echo.
pause
