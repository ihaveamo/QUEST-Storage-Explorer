@echo off
setlocal
cd /d "%~dp0"
title Quest 3 Storage Explorer
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-Quest3-Explorer.ps1" %*
set "EXITCODE=%ERRORLEVEL%"
if not "%EXITCODE%"=="0" (
    echo.
    echo Startup failed. Check the error above, then press any key to close.
    pause >nul
)
exit /b %EXITCODE%
