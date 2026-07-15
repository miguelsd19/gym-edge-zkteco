@echo off
setlocal

if "%~1"=="" (
  echo Uso:
  echo   install_with_nssm.bat "C:\ruta\nssm.exe"
  exit /b 1
)

set NSSM=%~1
set APP_DIR=%~dp0..
for %%I in ("%APP_DIR%") do set APP_DIR=%%~fI
set EXE_PATH=%APP_DIR%\dist\gym-edge-service-zkteco.exe
set SERVICE_NAME=GymEdgeZkteco

if not exist "%NSSM%" (
  echo No se encontro NSSM en %NSSM%
  exit /b 1
)

if not exist "%EXE_PATH%" (
  echo No se encontro el ejecutable en %EXE_PATH%
  exit /b 1
)

"%NSSM%" install "%SERVICE_NAME%" "%EXE_PATH%"
"%NSSM%" set "%SERVICE_NAME%" AppDirectory "%APP_DIR%\dist"
"%NSSM%" set "%SERVICE_NAME%" DisplayName "Gym Edge ZKTeco"
"%NSSM%" set "%SERVICE_NAME%" Description "Servicio local del torniquete ZKTeco para Gym System."
"%NSSM%" set "%SERVICE_NAME%" Start SERVICE_AUTO_START
"%NSSM%" set "%SERVICE_NAME%" AppExit Default Restart
"%NSSM%" set "%SERVICE_NAME%" AppThrottle 5000
"%NSSM%" set "%SERVICE_NAME%" AppRestartDelay 5000
"%NSSM%" start "%SERVICE_NAME%"

echo Servicio instalado como %SERVICE_NAME%.
