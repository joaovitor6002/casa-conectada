@echo off
setlocal
title Casa Conectada - Instalador
cd /d "%~dp0"

REM Precisa de administrador so para liberar a porta 5000 no firewall
net session >nul 2>&1
if errorlevel 1 (
  echo Pedindo permissao de administrador... clique em SIM.
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

set "DEST=%USERPROFILE%\CasaConectadaApp"
set "DADOS=%USERPROFILE%\CasaConectada"

echo.
echo ===== Instalando o Casa Conectada =====
echo.
echo [1/5] Conferindo o Windows...
if not exist "%SystemRoot%\System32\ucrtbase.dll" (
  echo.
  echo  FALTA UMA ATUALIZACAO DO WINDOWS 7 ^(Universal C Runtime, KB2999226^).
  echo  Instale o "Visual C++ 2015-2022 Redistributable x64" ou rode o
  echo  Windows Update e depois execute este instalador de novo.
  pause
  exit /b 1
)

echo [2/5] Copiando os arquivos para %DEST% ...
xcopy "%~dp0python" "%DEST%\python\" /E /I /Y /Q >nul
xcopy "%~dp0app" "%DEST%\app\" /E /I /Y /Q >nul
copy /Y "%~dp0atalhos\CASA CONECTADA.bat" "%DEST%\" >nul
copy /Y "%~dp0atalhos\ACHAR TVs.bat" "%DEST%\" >nul
copy /Y "%~dp0LEIA-ME.txt" "%DEST%\" >nul

echo [3/5] Dados (usuarios, aparelhos)...
if exist "%DADOS%\dispositivos.json" (
  echo       Ja existem dados em %DADOS%, mantive os de la.
) else (
  xcopy "%~dp0dados-iniciais" "%DADOS%\" /E /I /Y /Q >nul
  echo       Copiei os usuarios e aparelhos do PC principal.
)
if not exist "%DADOS%\atualizacao.json" (
  copy /Y "%~dp0dados-iniciais\atualizacao.json" "%DADOS%\" >nul
)
echo       Este notebook vai receber as atualizacoes do PC principal sozinho.

echo [4/5] Testando o Python...
"%DEST%\python\python.exe" -c "import flask, werkzeug, itsdangerous; print('      Python e Flask OK')"
if errorlevel 1 (
  echo.
  echo  O Python nao abriu. Rode o Windows Update ^(precisa do Windows 7 SP1
  echo  e da atualizacao KB2533623^) e execute este instalador de novo.
  pause
  exit /b 1
)

echo [5/5] Liberando a porta 5000 no firewall e criando os atalhos...
netsh advfirewall firewall delete rule name="Casa Conectada" >nul 2>&1
netsh advfirewall firewall add rule name="Casa Conectada" dir=in action=allow protocol=TCP localport=5000 profile=private,domain >nul
(
  echo @echo off
  echo call "%DEST%\CASA CONECTADA.bat"
) > "%USERPROFILE%\Desktop\CASA CONECTADA.bat"
(
  echo @echo off
  echo call "%DEST%\ACHAR TVs.bat"
) > "%USERPROFILE%\Desktop\ACHAR TVs.bat"

echo.
echo ===== Pronto! =====
echo Na Area de Trabalho agora tem "CASA CONECTADA" (liga o site)
echo e "ACHAR TVs" (procura as TVs na rede).
echo.
echo IMPORTANTE: a rede do notebook precisa ser "Rede domestica" ou
echo "Rede de trabalho" (nao "Rede publica"), senao os celulares nao entram.
echo.
pause
