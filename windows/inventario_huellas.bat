@echo off
setlocal

rem Inventario de huellas por usuario (solo lectura, sin plantillas).
rem Detiene el edge, lee el lector y SIEMPRE vuelve a arrancar el edge.
rem Uso (CMD como administrador, desde la carpeta del repo):
rem     windows\inventario_huellas.bat [IP_DEL_LECTOR]

set "READER_HOST=%~1"
if "%READER_HOST%"=="" set "READER_HOST=192.168.1.201"

net session >nul 2>&1
if not %errorlevel%==0 (
  echo Abre CMD como administrador: hace falta para detener y arrancar el servicio.
  exit /b 1
)

cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo No existe .venv. Sigue GUIA_INSTALACION_WINDOWS.md para crearlo.
  exit /b 1
)

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmm"') do set "STAMP=%%i"
if not exist pruebas mkdir pruebas
set "OUT=pruebas\huellas-%STAMP%.json"

echo Deteniendo GymEdgeZkteco (el lector acepta una sola conexion)...
sc stop GymEdgeZkteco >nul 2>&1
timeout /t 5 /nobreak >nul

".venv\Scripts\python.exe" -m edge_service_zkteco.fingerprint_inventory --host "%READER_HOST%" --out "%OUT%"
set "RESULT=%errorlevel%"

echo.
echo Arrancando GymEdgeZkteco de nuevo...
sc start GymEdgeZkteco >nul 2>&1
timeout /t 3 /nobreak >nul
sc query GymEdgeZkteco | findstr /i "STATE"

echo.
if "%RESULT%"=="0" echo RESULTADO 0: inventario guardado en %OUT%.
if "%RESULT%"=="1" echo RESULTADO 1: no se pudo conectar al lector (IP, red o el edge seguia conectado).
if "%RESULT%"=="2" echo RESULTADO 2: se conecto pero fallo la lectura. Vuelve a correrlo.
exit /b %RESULT%
