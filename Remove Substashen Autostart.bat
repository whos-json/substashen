@echo off
rem Double-click: stops Substashen starting at sign-in. A running copy keeps running until you close it.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\uninstall_autostart.ps1"
echo.
pause
