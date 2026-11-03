@echo off
setlocal

rem Prueba del registro remoto de huella y cara (enroll_probe) en el lector de la sucursal.
rem Detiene el edge, corre la prueba con --verbose guardando la salida y SIEMPRE
rem vuelve a arrancar el edge. Uso (CMD como administrador, desde la carpeta del repo):
rem     windows\probar_registro_huella.bat [IP_DEL_LECTOR] [huella^|cara^|ambas]

set "READER_HOST=%~1"
if "%READER_HOST%"=="" set "READER_HOST=192.168.1.201"
set "MODO=%~2"
if "%MODO%"=="" set "MODO=ambas"

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
set "LOG=pruebas\registro-huella-%STAMP%.log"

echo Deteniendo GymEdgeZkteco (el lector acepta una sola conexion)...
sc stop GymEdgeZkteco >nul 2>&1
timeout /t 5 /nobreak >nul

echo.
echo ============================================================
echo  Ten a alguien JUNTO AL LECTOR (modo %MODO%): primero el mismo
echo  dedo 3 veces y luego ponerse de frente para la cara.
echo  La salida se guarda en %LOG%
echo ============================================================
echo.

powershell -NoProfile -Command "& '.venv\Scripts\python.exe' -m edge_service_zkteco.enroll_probe --host '%READER_HOST%' --modo '%MODO%' --verbose 2>&1 | Tee-Object -FilePath '%LOG%'; exit $LASTEXITCODE"
set "RESULT=%errorlevel%"

echo.
echo Arrancando GymEdgeZkteco de nuevo...
sc start GymEdgeZkteco >nul 2>&1
timeout /t 3 /nobreak >nul
sc query GymEdgeZkteco | findstr /i "STATE"

echo.
if "%RESULT%"=="0" echo RESULTADO 0: el lector SI acepta el registro remoto (%MODO%).
if "%RESULT%"=="1" echo RESULTADO 1: no se pudo conectar al lector (IP, red o el edge seguia conectado).
if "%RESULT%"=="2" echo RESULTADO 2: el lector NO guardo la huella o la cara. Comparte %LOG%.
if "%RESULT%"=="3" echo RESULTADO 3: el ID de prueba 990001 ya existe en el lector. No se toco nada.
if "%RESULT%"=="4" echo RESULTADO 4: error inesperado. Comparte %LOG%.
echo Salida completa en %LOG%
exit /b %RESULT%
