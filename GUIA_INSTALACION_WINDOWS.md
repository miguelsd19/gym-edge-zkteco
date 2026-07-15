# Guia Simple Para Instalar El Edge ZKTeco En Una PC Windows Nueva

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

Necesitas estas 5 cosas:

1. Una PC Windows conectada a la misma red que el terminal ZKTeco.
2. La carpeta `edge_service_zkteco` ya copiada en la PC.
3. El archivo `dist\.env` correcto para esa sucursal (con la IP del terminal en `ZK_DEVICE_HOST`).
4. El archivo `dist\gym-edge-service-zkteco.exe`.
5. `NSSM` descargado en la PC para instalar los servicios.

Si el backend esta en la nube y quieres que la apertura remota desde el sistema funcione,
necesitas tambien:

6. `ngrok` instalado en la PC.
7. Un authtoken valido de `ngrok`.

## Configuracion previa del terminal ZKTeco

Antes de instalar el edge, el terminal debe quedar asi (lo hace soporte una sola vez):

1. IP fija en la misma red que la PC (la que va en `ZK_DEVICE_HOST`).
2. Puerto de comunicacion `4370` (es el default de ZKTeco).
3. Si el terminal tiene comm key configurada, esa clave va en `ZK_DEVICE_PASSWORD`.
4. Crear el grupo de acceso bloqueado: menu `Access Control` → `Access Groups`,
   grupo `99` **sin horario de acceso valido**. A ese grupo se mueven los miembros vencidos.
5. Enrolar a los usuarios usando el mismo `User ID` que tiene el miembro en Gym System
   (`device_user_id`).

## Carpeta que se va a usar

En esta guia asumimos que la carpeta esta aqui:

```bat
C:\edge_service_zkteco
```

Si la pusiste en otro lugar, solo cambia esa ruta en los comandos.

## Instalacion Inicial

### Paso 1. Abrir CMD

Abre `Símbolo del sistema` en Windows.

### Paso 2. Entrar a la carpeta del edge

Pega esto y presiona Enter:

```bat
cd /d C:\edge_service_zkteco
```

### Paso 3. Verificar que exista el ejecutable y el .env

Pega esto y presiona Enter:

```bat
dir dist\gym-edge-service-zkteco.exe dist\.env
```

Si aparecen ambos archivos, continua.

Si no aparecen, pide soporte porque falta copiar el ejecutable o el `.env`.

### Paso 4. Probar el edge manualmente una vez

Pega esto y presiona Enter:

```bat
dist\gym-edge-service-zkteco.exe
```

Al arrancar debes ver algo parecido a:

- `ZKTeco edge service started...`
- `Connected to ZKTeco device 192.168.x.x:4370.`
- listener control en `9877`

Si quieres cerrarlo despues de la prueba, presiona:

```bat
Control + C
```

### Paso 5. Configurar ngrok si el backend esta en la nube

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

### Paso 6. Instalar el servicio del edge con NSSM

Asumiendo que `nssm.exe` esta en `C:\tools\nssm\nssm.exe`, pega esto:

```bat
windows\install_with_nssm.bat "C:\tools\nssm\nssm.exe"
```

Si todo sale bien, veras un mensaje parecido a:

```bat
Servicio instalado como GymEdgeZkteco.
```

Este servicio queda en arranque automatico y, si el proceso se cae, NSSM intentara levantarlo otra vez.

### Paso 7. Instalar ngrok como servicio automatico

Si tu backend esta en la nube, instala tambien `ngrok` como servicio para no depender
de abrir una consola manualmente en cada reinicio.

Asumiendo que `ngrok.exe` esta en `C:\tools\ngrok\ngrok.exe`, pega esto:

```bat
windows\install_ngrok_with_nssm.bat "C:\tools\nssm\nssm.exe" "C:\tools\ngrok\ngrok.exe"
```

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

- miembro activo → grupo `1` (normal)
- miembro vencido o suspendido → grupo `99` (bloqueado, sin horario de acceso)

Esto corre:

1. al arrancar el edge
2. despues cada 24 horas por default
3. al instante cuando el sistema renueva o vence una membresia (via apertura del backend)

El sync **no borra usuarios ni caras/huellas**: solo cambia el grupo.

Verificacion recomendada la primera vez (con un usuario de prueba):

1. Enrola una cara de prueba en el terminal.
2. Vence o suspende su membresia en el sistema y espera el sync (o reinicia el servicio).
3. El terminal debe seguir reconociendo la cara pero NO abrir.
4. Reactiva la membresia: la puerta debe abrir de nuevo, sin re-enrolar.

Si necesitas forzar el sync de inmediato, reinicia el servicio:

```bat
windows\restart_services.bat
```

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

Si los eventos si llegan al backend pero la apertura remota no genera nada nuevo en el log del edge,
revisa primero si `ngrok` sigue corriendo.

## Checklist Rapido

Si solo quieres la version corta, estos son los pasos:

```bat
cd /d C:\edge_service_zkteco
dir dist\gym-edge-service-zkteco.exe dist\.env
dist\gym-edge-service-zkteco.exe
windows\install_with_nssm.bat "C:\tools\nssm\nssm.exe"
sc query GymEdgeZkteco
```

Si la apertura remota usara `ngrok`, instala tambien:

```bat
windows\install_ngrok_with_nssm.bat "C:\tools\nssm\nssm.exe" "C:\tools\ngrok\ngrok.exe"
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
