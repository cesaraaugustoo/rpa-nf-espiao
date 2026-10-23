@echo off
rem Executa o RPA na hora, mostrando o andamento. Aceita os mesmos parametros do modulo,
rem por exemplo:  EXECUTAR_AGORA.bat --visivel   ou   EXECUTAR_AGORA.bat --pendencias
setlocal
chcp 65001 >nul
cd /d "%~dp0"
"%LOCALAPPDATA%\RPA_NF_ESP\venv\Scripts\python.exe" -m rpa_nf_esp %*
set CODIGO=%ERRORLEVEL%
echo.
echo Codigo de saida: %CODIGO%  (0 = ok, 1 = com falhas, 2 = erro fatal, 3 = ja estava rodando)
pause
exit /b %CODIGO%
