@echo off
title Casa Conectada - procurando TVs
cd /d "%~dp0app\servidor"
set "ADB_PATH=%~dp0app\platform-tools\adb.exe"
"%~dp0python\python.exe" -X utf8 servidor.py --procurar
echo.
pause
