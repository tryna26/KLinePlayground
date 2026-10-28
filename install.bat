@echo off
setlocal
powershell.exe -NoProfile -File "%~dp0scripts\build_windows.ps1" %*
exit /b %errorlevel%
