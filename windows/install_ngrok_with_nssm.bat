@echo off
setlocal

if "%~1"=="" (
  echo Uso:
  echo   install_ngrok_with_nssm.bat "C:\ruta\nssm.exe" "C:\ruta\ngrok.exe"
  exit /b 1
)

if "%~2"=="" (
  echo Uso:
  echo   install_ngrok_with_nssm.bat "C:\ruta\nssm.exe" "C:\ruta\ngrok.exe"
  exit /b 1
)

set NSSM=%~1
set NGROK=%~2
set APP_DIR=%~dp0..
for %%I in ("%APP_DIR%") do set APP_DIR=%%~fI
set DIST_DIR=%APP_DIR%\dist
set RUNTIME_DIR=%DIST_DIR%\runtime
set SERVICE_NAME=GymEdgeZktecoNgrok

if not exist "%NSSM%" (
  echo No se encontro NSSM en %NSSM%
  exit /b 1
)

if not exist "%NGROK%" (
  echo No se encontro ngrok en %NGROK%
  exit /b 1
)

if not exist "%RUNTIME_DIR%" (
  mkdir "%RUNTIME_DIR%"
)

"%NSSM%" install "%SERVICE_NAME%" "%NGROK%" http 9877 --log stdout
"%NSSM%" set "%SERVICE_NAME%" AppDirectory "%DIST_DIR%"
"%NSSM%" set "%SERVICE_NAME%" DisplayName "Gym Edge ZKTeco Ngrok"
"%NSSM%" set "%SERVICE_NAME%" Description "Tunel ngrok para apertura remota del edge ZKTeco."
"%NSSM%" set "%SERVICE_NAME%" Start SERVICE_AUTO_START
"%NSSM%" set "%SERVICE_NAME%" AppStdout "%RUNTIME_DIR%\ngrok.stdout.log"
"%NSSM%" set "%SERVICE_NAME%" AppStderr "%RUNTIME_DIR%\ngrok.stderr.log"
"%NSSM%" set "%SERVICE_NAME%" AppRotateFiles 1
"%NSSM%" set "%SERVICE_NAME%" AppRotateOnline 1
"%NSSM%" set "%SERVICE_NAME%" AppRotateBytes 1048576
"%NSSM%" set "%SERVICE_NAME%" AppExit Default Restart
"%NSSM%" set "%SERVICE_NAME%" AppThrottle 5000
"%NSSM%" set "%SERVICE_NAME%" AppRestartDelay 5000
"%NSSM%" start "%SERVICE_NAME%"

echo Servicio instalado como %SERVICE_NAME%.
echo Revisa la URL publica en %RUNTIME_DIR%\ngrok.stdout.log
