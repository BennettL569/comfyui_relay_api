@echo off
rem Double-click to create the relay manager shortcuts. See create_shortcut.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_shortcut.ps1" %*
pause
