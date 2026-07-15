@echo off
setlocal

cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
  echo No se encontro .venv\Scripts\python.exe
  echo Crea un entorno virtual en Windows e instala las dependencias primero:
  echo   python -m venv .venv
  echo   .venv\Scripts\pip install pyzk pyinstaller
  exit /b 1
)

".venv\Scripts\python.exe" -m PyInstaller ^
  --clean ^
  --noconfirm ^
  gym-edge-service-zkteco.spec

if errorlevel 1 exit /b %errorlevel%

echo.
echo Build listo en dist\gym-edge-service-zkteco.exe
echo Copia tambien tu archivo .env junto al .exe.
