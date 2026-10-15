# Guia Simple Para Instalar El Edge ZKTeco En Una PC Windows Nueva

> Si esta instalacion la hace un agente de IA, debe leer antes [AGENTS.md](AGENTS.md).

Esta guia esta pensada para que cualquier persona pueda dejar funcionando el edge del torniquete sin saber programacion.

## Que hace este edge

Este programa conecta la PC con el lector ZKTeco (SpeedFace) y con Gym System.

Su trabajo es:

- recibir los eventos del torniquete
- validar accesos en Gym System
- mandar la orden para abrir
- bloquear en el terminal a los miembros vencidos
- guardar logs para soporte

## Antes de empezar

Ten a la mano:

1. Una PC Windows (10 u 11) conectada **por cable** a la misma red que el lector ZKTeco, con internet.
2. Una cuenta de GitHub con acceso al repo `miguelsd19/gym-edge-zkteco` (es privado).
3. El archivo `.env` de esa sucursal. **No viene en el repo** porque lleva el `DEVICE_TOKEN`
   del punto de acceso; pidelo a soporte y traelo en USB o por un gestor de contrasenas.
4. Permiso de administrador en la PC (los servicios se instalan como administrador).
5. Si el backend esta en la nube (Railway) y quieres abrir el torniquete desde la app: una
   cuenta de `ngrok` y su authtoken.

Toma unos 30 a 45 minutos.

> **Importante:** el lector acepta **una sola conexion a la vez**. Si la sucursal venia usando
> otro sistema (por ejemplo ProGym), ese programa o su conexion en la nube deben estar apagados
> antes de arrancar el edge. Si es una migracion, sigue antes la
> [guia de respaldo del lector](GUIA_RESPALDO_PROGYM.md).

## Configuracion previa del terminal ZKTeco

Antes de instalar el edge, el terminal debe quedar asi (lo hace soporte una sola vez):

1. IP fija en la misma red que la PC (la que va en `ZK_DEVICE_HOST`).
2. Puerto de comunicacion `4370` (es el default de ZKTeco).
3. Si el terminal tiene comm key configurada, esa clave va en `ZK_DEVICE_PASSWORD`.
4. Crear el grupo de acceso bloqueado: menu `Access Control` → `Access Groups`,
   grupo `99` **sin horario de acceso valido**. A ese grupo se mueven los miembros vencidos,
   que ademas quedan deshabilitados en el lector.
5. Enrolar a los usuarios usando como `User ID` el **ID biometrico** que muestra la ficha del
   miembro en Gym System (con `ACCESS_MODE=rfid`, el campo `rfid_code`).

## Instalacion Inicial

En esta guia todo queda en `C:\edge_service_zkteco` y las herramientas en `C:\tools\`.
Si usas otras carpetas, cambia esas rutas en los comandos.

### Paso 1. Instalar los programas necesarios

1. **Git**: descarga e instala https://git-scm.com/download/win (deja las opciones por defecto).
2. **Python 3.11**: descarga e instala https://www.python.org/downloads/windows/ (version 3.11).
   En la primera pantalla del instalador **marca "Add python.exe to PATH"**.
3. **NSSM** (para dejar el edge como servicio de Windows):
   - Descarga el zip de https://nssm.cc/download (la version mas reciente).
   - Copia el archivo `win64\nssm.exe` del zip a `C:\tools\nssm\nssm.exe`.
4. **ngrok** (solo si el backend esta en la nube):
   - Descarga el zip de Windows de https://ngrok.com/download.
   - Copia `ngrok.exe` a `C:\tools\ngrok\ngrok.exe`.

Cierra y vuelve a abrir cualquier ventana de CMD despues de instalar, para que tome los cambios.

### Paso 2. Bajar el codigo desde GitHub

Abre `Símbolo del sistema` (CMD) y pega:

```bat
git clone https://github.com/miguelsd19/gym-edge-zkteco.git C:\edge_service_zkteco
cd /d C:\edge_service_zkteco
```

Como el repo es privado, la primera vez se abre una ventana para iniciar sesion en GitHub.
Usa una cuenta con acceso al repo (de preferencia una de solo lectura para esta PC, no la
cuenta personal de nadie).

### Paso 3. Construir el ejecutable

En la misma ventana de CMD, dentro de `C:\edge_service_zkteco`, pega uno por uno:

```bat
py -3.11 -m venv .venv
.venv\Scripts\pip install pyzk pyinstaller
windows\build_windows.bat
```

Al final debe decir `Build listo en dist\gym-edge-service-zkteco.exe`.

Si `py` no se reconoce, Python no quedo en el PATH: reinstalalo marcando "Add python.exe to PATH".

### Paso 4. Poner el archivo .env

Copia el `.env` de la sucursal a `C:\edge_service_zkteco\dist\.env` (junto al `.exe`). Por
ejemplo, si viene en una USB en `E:`:

```bat
copy E:\.env C:\edge_service_zkteco\dist\.env
```

Si no tienes el archivo, crealo a partir de `.env.example` y pide a soporte los valores. Revisa
que al menos tenga bien:

- `BACKEND_BASE_URL`: la URL del backend de produccion.
- `DEVICE_TOKEN`: el token del punto de acceso de esta sucursal (en el admin de Gym System).
- `ACCESS_MODE=rfid`
- `ZK_DEVICE_HOST`: la IP del lector, por ejemplo `192.168.1.201`.

Verifica que esten el ejecutable y el `.env`:

```bat
dir dist\gym-edge-service-zkteco.exe dist\.env
```

### Paso 5. Probar el edge manualmente una vez

Primero confirma que la PC llega al lector (cambia la IP por la de tu lector):

```bat
powershell -Command "Test-NetConnection 192.168.1.201 -Port 4370"
```

Debe decir `TcpTestSucceeded : True`. Luego arranca el edge:

```bat
dist\gym-edge-service-zkteco.exe
```

Al arrancar debes ver algo parecido a:

- `ZKTeco edge service started...`
- `Connected to ZKTeco device 192.168.x.x:4370.`
- listener control en `9877`

Haz pasar a un miembro activo por el lector y revisa que entre. Para cerrar la prueba presiona
`Control + C`.

### Paso 6. Configurar ngrok si el backend esta en la nube

Si tu backend productivo esta en Railway o en otro hosting fuera de la sucursal,
la IP local de esta PC no sera visible desde internet. En ese caso debes exponer `9877`
con `ngrok`.

Primero guarda el authtoken:

```bat
ngrok config add-authtoken TU_AUTHTOKEN
```

Luego abre el tunel:

```bat
ngrok http 9877
```

Debes ver una URL como esta:

```text
https://mi-sucursal.ngrok-free.dev
```

Con esa URL:

1. En el sistema, `Edge control url` debe quedar como `https://mi-sucursal.ngrok-free.dev`
2. No agregues `/door/open`
3. En el backend productivo, `ACCESS_EDGE_CONTROL_ALLOWED_HOSTS` debe incluir solo el host:

```env
mi-sucursal.ngrok-free.dev
```

Si cierras `ngrok`, la apertura remota dejara de funcionar.

### Paso 7. Instalar el servicio del edge con NSSM

Abre CMD **como administrador** (clic derecho en "Símbolo del sistema" → "Ejecutar como
administrador"), entra a la carpeta y pega:

```bat
cd /d C:\edge_service_zkteco
windows\install_with_nssm.bat "C:\tools\nssm\nssm.exe"
```

Si todo sale bien, veras un mensaje parecido a:

```bat
Servicio instalado como GymEdgeZkteco.
```

Este servicio queda en arranque automatico y, si el proceso se cae, NSSM intentara levantarlo otra vez.

### Paso 8. Instalar ngrok como servicio automatico

Si tu backend esta en la nube, instala tambien `ngrok` como servicio para no depender
de abrir una consola manualmente en cada reinicio.

En el CMD de administrador pega:

```bat
windows\install_ngrok_with_nssm.bat "C:\tools\nssm\nssm.exe" "C:\tools\ngrok\ngrok.exe"
"C:\tools\nssm\nssm.exe" set GymEdgeZktecoNgrok AppEnvironmentExtra NGROK_AUTHTOKEN=TU_AUTHTOKEN
"C:\tools\nssm\nssm.exe" restart GymEdgeZktecoNgrok
```

La segunda linea es necesaria: el servicio corre con la cuenta del sistema, que **no lee** el
authtoken guardado con `ngrok config add-authtoken` en tu usuario. Sin ella el tunel no arranca.

Si todo sale bien, veras un mensaje parecido a:

```bat
Servicio instalado como GymEdgeZktecoNgrok.
```

La URL publica de `ngrok` quedara registrada en:

```bat
dist\runtime\ngrok.stdout.log
```

Importante:

1. Si `ngrok` gratis cambia de URL despues de un reinicio, actualiza `Edge control url`.
2. Si cambia de host, actualiza tambien `ACCESS_EDGE_CONTROL_ALLOWED_HOSTS` en el backend.

## Bloqueo de miembros vencidos

El edge consulta al backend que miembros deben quedar permitidos o bloqueados y los mueve
de grupo dentro del terminal ZKTeco:

- miembro activo → grupo `1` (normal) y habilitado
- miembro vencido o suspendido → grupo `99` (sin horario de acceso) y deshabilitado

Esto corre:

1. al arrancar el edge
2. despues cada 24 horas por default
3. al instante cuando el sistema renueva o vence una membresia (via apertura del backend)

El sync **no borra usuarios ni caras/huellas**: solo cambia el grupo y habilita o deshabilita al usuario.

Verificacion recomendada la primera vez (con un usuario de prueba):

1. Enrola una cara de prueba en el terminal.
2. Vence o suspende su membresia en el sistema y espera el sync (o reinicia el servicio).
3. El terminal debe seguir reconociendo la cara pero NO abrir.
4. Reactiva la membresia: la puerta debe abrir de nuevo, sin re-enrolar.

Si necesitas forzar el sync de inmediato, reinicia el servicio:

```bat
windows\restart_services.bat
```

## Como actualizar el edge

Cuando haya una version nueva, en CMD **como administrador**:

```bat
cd /d C:\edge_service_zkteco
sc stop GymEdgeZkteco
git pull
.venv\Scripts\pip install pyzk pyinstaller
windows\build_windows.bat
sc start GymEdgeZkteco
```

Hay que detener el servicio antes de construir: Windows no deja reemplazar un `.exe` que esta
corriendo. El `dist\.env` y los logs de `dist\runtime\` se conservan.

## Como revisar si esta corriendo

Pega esto y presiona Enter:

```bat
sc query GymEdgeZkteco
```

Si ves `STATE` como `RUNNING`, el servicio esta corriendo.

Si tambien instalaste `ngrok`, revisa:

```bat
sc query GymEdgeZktecoNgrok
```

## Como ver los logs

### Log principal del edge

Pega esto y presiona Enter:

```bat
type dist\runtime\edge_zkteco.log
```

Si quieres ver solo las ultimas lineas:

```bat
powershell -Command "Get-Content dist\runtime\edge_zkteco.log -Tail 50"
```

### Log de ngrok

```bat
powershell -Command "Get-Content dist\runtime\ngrok.stdout.log -Tail 50"
```

### Carpeta de logs

Los logs y archivos de runtime quedan en:

```bat
dist\runtime\
```

## Como reiniciarlo

### Reinicio rapido de edge + ngrok

Pega esto y presiona Enter:

```bat
windows\restart_services.bat
```

### Reinicio manual por servicio

Pega estos comandos, uno por uno:

```bat
sc stop GymEdgeZkteco
```

```bat
sc start GymEdgeZkteco
```

Si tambien usas `ngrok`:

```bat
sc stop GymEdgeZktecoNgrok
```

```bat
sc start GymEdgeZktecoNgrok
```

## Como desinstalarlo

Si necesitas quitar el servicio y tienes `nssm.exe`, pega esto:

```bat
"C:\tools\nssm\nssm.exe" remove GymEdgeZkteco confirm
```

## Que hacer si no funciona

Revisa esto en este orden:

1. La PC debe estar conectada a internet y a la misma red del terminal ZKTeco.
2. El torniquete debe estar encendido y responder en el puerto `4370`
   (`powershell -Command "Test-NetConnection IP-DEL-TERMINAL -Port 4370"` debe decir `TcpTestSucceeded : True`).
3. El archivo `dist\.env` debe existir y tener la IP correcta en `ZK_DEVICE_HOST`.
4. Si el terminal tiene comm key, `ZK_DEVICE_PASSWORD` debe coincidir.
5. El servicio debe aparecer en:

```bat
sc query GymEdgeZkteco
```

Si usas backend en la nube, `ngrok` tambien debe aparecer en:

```bat
sc query GymEdgeZktecoNgrok
```

6. Revisa los logs:

```bat
powershell -Command "Get-Content dist\runtime\edge_zkteco.log -Tail 50"
```

Si ves `Could not connect to ZKTeco device...` repetido, es un problema de red, IP o comm key.
Si el lector responde en el puerto `4370` pero el edge igual no conecta, revisa que ningun otro
programa este conectado al lector (por ejemplo ProGym): solo acepta una conexion a la vez.

Si los eventos si llegan al backend pero la apertura remota no genera nada nuevo en el log del edge,
revisa primero si `ngrok` sigue corriendo.

## Checklist Rapido

Si solo quieres la version corta, estos son los pasos:

```bat
git clone https://github.com/miguelsd19/gym-edge-zkteco.git C:\edge_service_zkteco
cd /d C:\edge_service_zkteco
py -3.11 -m venv .venv
.venv\Scripts\pip install pyzk pyinstaller
windows\build_windows.bat
copy E:\.env dist\.env
dist\gym-edge-service-zkteco.exe
```

Despues, en CMD como administrador:

```bat
cd /d C:\edge_service_zkteco
windows\install_with_nssm.bat "C:\tools\nssm\nssm.exe"
sc query GymEdgeZkteco
```

Si la apertura remota usara `ngrok`, instala tambien:

```bat
windows\install_ngrok_with_nssm.bat "C:\tools\nssm\nssm.exe" "C:\tools\ngrok\ngrok.exe"
"C:\tools\nssm\nssm.exe" set GymEdgeZktecoNgrok AppEnvironmentExtra NGROK_AUTHTOKEN=TU_AUTHTOKEN
"C:\tools\nssm\nssm.exe" restart GymEdgeZktecoNgrok
```

## Datos importantes para soporte

Si algo falla y necesitas mandar evidencia, comparte:

- captura del error en CMD
- resultado de:

```bat
sc query GymEdgeZkteco
```

- resultado de:

```bat
powershell -Command "Get-Content dist\runtime\edge_zkteco.log -Tail 100"
```

- resultado de:

```bat
powershell -Command "Get-Content dist\runtime\ngrok.stdout.log -Tail 100"
```

- URL publica actual de `ngrok` si aplica
- sucursal donde esta instalado
- IP del terminal ZKTeco

## Nota final

Una vez instalado, no hace falta abrir el `.exe` manualmente.

Windows levantara el edge como servicio al iniciar la PC. Si tambien instalas `ngrok`
como servicio, ambos arrancaran en segundo plano y podras reiniciarlos con
`windows\restart_services.bat`.
