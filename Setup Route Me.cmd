@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
set "ROUTE_ME_EXIT=%ERRORLEVEL%"
pause
exit /b %ROUTE_ME_EXIT%
