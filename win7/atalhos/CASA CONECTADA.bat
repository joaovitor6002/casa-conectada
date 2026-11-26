@echo off
title Casa Conectada - servidor (deixe esta janela aberta)
cd /d "%~dp0app\servidor"
set "ADB_PATH=%~dp0app\platform-tools\adb.exe"
:ligar
echo Ligando o servidor... deixe esta janela aberta.
echo Para desligar: Ctrl+C ou feche a janela.
echo.
"%~dp0python\python.exe" -X utf8 servidor.py
if errorlevel 3 if not errorlevel 4 (
  echo.
  echo Atualizacao instalada! Religando o servidor...
  timeout /t 3 >nul
  goto ligar
)
echo.
echo O servidor parou. Se apareceu erro acima, tire um print e mande pro Claude.
pause
