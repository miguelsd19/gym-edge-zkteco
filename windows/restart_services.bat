@echo off
setlocal

echo Reiniciando servicios del edge...

sc query GymEdgeZktecoNgrok >nul 2>&1
if %errorlevel%==0 (
  echo Reiniciando GymEdgeZktecoNgrok...
  sc stop GymEdgeZktecoNgrok >nul
  timeout /t 2 /nobreak >nul
  sc start GymEdgeZktecoNgrok
) else (
  echo GymEdgeZktecoNgrok no esta instalado. Se omite.
)

sc query GymEdgeZkteco >nul 2>&1
if %errorlevel%==0 (
  echo Reiniciando GymEdgeZkteco...
  sc stop GymEdgeZkteco >nul
  timeout /t 2 /nobreak >nul
  sc start GymEdgeZkteco
) else (
  echo GymEdgeZkteco no esta instalado. Se omite.
)

echo Reinicio solicitado.
