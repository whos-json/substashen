@echo off
rem Double-click: makes Substashen start at sign-in (shown in Settings > Apps > Startup) and starts it now.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_autostart.ps1"
echo.
pause
