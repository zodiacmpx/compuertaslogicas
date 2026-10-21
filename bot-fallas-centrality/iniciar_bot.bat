@echo off
rem Arranca el bot de fallas con config.json de esta misma carpeta.
rem Si el bot termina con error, la ventana queda abierta (pause) para leer el motivo.
setlocal
cd /d "%~dp0"

if not exist "config.json" (
  echo No existe config.json en %CD%
  echo Copia config.example.json como config.json y editalo antes de arrancar.
  pause
  exit /b 2
)

rem En PCs corporativos "python" puede no estar en el PATH pero si el lanzador "py".
set "PY=python"
where py >nul 2>nul && set "PY=py -3"

%PY% -m bot_fallas --config config.json
set "CODIGO=%ERRORLEVEL%"
if not "%CODIGO%"=="0" (
  echo.
  echo El bot termino con codigo %CODIGO%. Revisa el mensaje de arriba y datos\bot.log
  pause
)
exit /b %CODIGO%
