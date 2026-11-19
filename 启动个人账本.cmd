@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\start_personal.ps1" %*
if errorlevel 1 pause
