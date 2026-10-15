# Guia Simple Para Instalar El Edge ZKTeco En Una Mac Nueva

Esta guia esta pensada para que cualquier persona pueda dejar funcionando el edge del torniquete sin saber programacion.

## Que hace este edge

Este programa conecta la computadora con el lector ZKTeco (SpeedFace) y con Gym System.

Su trabajo es:

- recibir los eventos del torniquete
- validar accesos en Gym System
- mandar la orden para abrir
- bloquear en el terminal a los miembros vencidos
- guardar logs para soporte

## Antes de empezar

Necesitas estas 4 cosas:

1. Una Mac conectada a la misma red que el terminal ZKTeco.
2. El codigo del edge en `/Users/Shared/edge_service_zkteco` (se clona de GitHub, ver "Paso 0").
3. El archivo `.env` correcto para esa sucursal (con la IP del terminal en `ZK_DEVICE_HOST`).
4. Tener abierta la Terminal de Mac.

Si el backend esta en la nube y quieres que la apertura remota desde el sistema funcione,
necesitas tambien:

5. `ngrok` instalado en la Mac.
6. Un authtoken valido de `ngrok`.

## Configuracion previa del terminal ZKTeco

Antes de instalar el edge, el terminal debe quedar asi (lo hace soporte una sola vez):

1. IP fija en la misma red que la Mac (la que va en `ZK_DEVICE_HOST`).
2. Puerto de comunicacion `4370` (es el default de ZKTeco).
3. Si el terminal tiene comm key configurada, esa clave va en `ZK_DEVICE_PASSWORD`.
4. Crear el grupo de acceso bloqueado: menu `Access Control` → `Access Groups`,
   grupo `99` **sin horario de acceso valido**. A ese grupo se mueven los miembros vencidos, que ademas quedan deshabilitados en el lector.
5. Enrolar a los usuarios usando como `User ID` el **ID biometrico** que muestra la ficha del
   miembro en Gym System (con `ACCESS_MODE=rfid`, el campo `rfid_code`).

## Paso 0. Bajar el codigo desde GitHub (solo la primera vez)

```bash
git clone https://github.com/miguelsd19/gym-edge-zkteco.git /Users/Shared/edge_service_zkteco
```

El repo es privado: Git pedira iniciar sesion con una cuenta de GitHub que tenga acceso. El `.env`
**no viene en el repo** (lleva el `DEVICE_TOKEN`): copialo por un medio seguro o crealo desde
`.env.example`. Para actualizar despues: `git pull` en esa carpeta, reconstruir y reiniciar.

## Carpeta que se va a usar

En esta guia asumimos que la carpeta esta aqui:

```bash
/Users/Shared/edge_service_zkteco
```

Si la pusiste en otro lugar, solo cambia esa ruta en los comandos.

## Instalacion Inicial

### Paso 1. Abrir Terminal

Abre la aplicacion `Terminal` en la Mac.

### Paso 2. Entrar a la carpeta del edge

Pega esto y presiona Enter:

```bash
cd /Users/Shared/edge_service_zkteco
```

### Paso 3. Dar permiso a los scripts

Pega esto y presiona Enter:

```bash
chmod +x macos/build_macos.sh macos/install_launchd.sh macos/uninstall_launchd.sh macos/install_ngrok_launchd.sh macos/uninstall_ngrok_launchd.sh macos/restart_launchd.sh
```

### Paso 4. Verificar que exista el ejecutable

Pega esto y presiona Enter:

```bash
ls -l dist/gym-edge-service-zkteco dist/.env
```

Si aparecen ambos archivos, continua.

Si no aparecen, pide soporte porque falta copiar el ejecutable o el `.env`.

### Paso 5. Instalar el servicio del edge

Pega esto y presiona Enter:

```bash
./macos/install_launchd.sh
```

Si todo sale bien, veras un mensaje parecido a:

```bash
Servicio instalado en launchd como com.gymsystem.edge.zkteco
```

Este servicio queda en arranque automatico al iniciar sesion y `launchd` intentara volver
a levantarlo si se cae.

### Paso 6. Configurar ngrok si el backend esta en la nube

Si tu backend productivo esta en Railway o en otro hosting fuera de la sucursal,
la IP local de esta Mac no sera visible desde internet. En ese caso debes exponer `9877`
con `ngrok`.

Primero guarda el authtoken:

```bash
ngrok config add-authtoken TU_AUTHTOKEN
```

Luego abre el tunel:

```bash
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

### Paso 7. Instalar ngrok como servicio automatico

Para no depender de dejar una Terminal abierta en cada reinicio, instala tambien `ngrok`
en `launchd`.

Si `ngrok` ya esta en tu `PATH`, pega esto:

```bash
./macos/install_ngrok_launchd.sh
```

Si prefieres pasar la ruta manualmente:

```bash
./macos/install_ngrok_launchd.sh /ruta/completa/a/ngrok
```

La URL publica de `ngrok` quedara registrada en:

```bash
dist/runtime/ngrok.stdout.log
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

El sync **no borra usuarios ni caras/huellas**: solo cambia el grupo.

Verificacion recomendada la primera vez (con un usuario de prueba):

1. Enrola una cara de prueba en el terminal.
2. Vence o suspende su membresia en el sistema y espera el sync (o reinicia el servicio).
3. El terminal debe seguir reconociendo la cara pero NO abrir.
4. Reactiva la membresia: la puerta debe abrir de nuevo, sin re-enrolar.

Si necesitas forzar el sync de inmediato, reinicia el servicio:

```bash
./macos/restart_launchd.sh
```

## Como revisar si esta corriendo

Pega esto y presiona Enter:

```bash
launchctl list | grep com.gymsystem.edge.zkteco
```

Si sale una linea con `com.gymsystem.edge.zkteco`, el servicio esta instalado.

Si tambien instalaste `ngrok`, en esa misma salida debe aparecer
`com.gymsystem.edge.zkteco.ngrok`.

## Como ver los logs

### Log principal del edge

```bash
tail -f dist/runtime/edge_zkteco.log
```

Al arrancar debes ver algo parecido a:

- `ZKTeco edge service started...`
- `Connected to ZKTeco device 192.168.x.x:4370.`
- listener control en `9877`

### Log extra del servicio de Mac

```bash
tail -f dist/runtime/launchd.stdout.log
```

### Log de ngrok

```bash
tail -f dist/runtime/ngrok.stdout.log
```

Si quieres salir de esa pantalla, presiona:

```bash
Control + C
```

## Como reiniciarlo

### Reinicio rapido de edge + ngrok

```bash
./macos/restart_launchd.sh
```

### Reinicio manual por servicio

Si el edge necesita reiniciarse, pega estos 2 comandos, uno por uno:

```bash
launchctl unload "$HOME/Library/LaunchAgents/com.gymsystem.edge.zkteco.plist"
```

```bash
launchctl load "$HOME/Library/LaunchAgents/com.gymsystem.edge.zkteco.plist"
```

Si tambien usas `ngrok`:

```bash
launchctl unload "$HOME/Library/LaunchAgents/com.gymsystem.edge.zkteco.ngrok.plist"
```

```bash
launchctl load "$HOME/Library/LaunchAgents/com.gymsystem.edge.zkteco.ngrok.plist"
```

## Como desinstalarlo

Si necesitas quitar el servicio:

```bash
cd /Users/Shared/edge_service_zkteco
./macos/uninstall_launchd.sh
```

Si tambien quieres quitar `ngrok` de inicio automatico:

```bash
cd /Users/Shared/edge_service_zkteco
./macos/uninstall_ngrok_launchd.sh
```

## Que hacer si no funciona

Revisa esto en este orden:

1. La Mac debe estar conectada a internet y a la misma red del terminal ZKTeco.
2. El torniquete debe estar encendido y responder en el puerto `4370`
   (`nc -vz IP-DEL-TERMINAL 4370` debe decir `succeeded`).
3. El archivo `dist/.env` debe existir y tener la IP correcta en `ZK_DEVICE_HOST`.
4. Si el terminal tiene comm key, `ZK_DEVICE_PASSWORD` debe coincidir.
5. El servicio debe aparecer en:

```bash
launchctl list | grep com.gymsystem.edge.zkteco
```

6. Revisa los logs:

```bash
tail -n 50 dist/runtime/edge_zkteco.log
```

Si ves `Could not connect to ZKTeco device...` repetido, es un problema de red, IP o comm key.

Si los eventos si llegan al backend pero la apertura remota no genera nada nuevo en el log del edge,
revisa primero si `ngrok` sigue corriendo.

## Checklist Rapido

Si solo quieres la version corta, estos son los pasos:

```bash
cd /Users/Shared/edge_service_zkteco
chmod +x macos/build_macos.sh macos/install_launchd.sh macos/uninstall_launchd.sh macos/install_ngrok_launchd.sh macos/uninstall_ngrok_launchd.sh macos/restart_launchd.sh
ls -l dist/gym-edge-service-zkteco dist/.env
./macos/install_launchd.sh
launchctl list | grep com.gymsystem.edge.zkteco
```

Si la apertura remota usara `ngrok`, instala tambien:

```bash
./macos/install_ngrok_launchd.sh
```

## Datos importantes para soporte

Si algo falla y necesitas mandar evidencia, comparte:

- captura del error en Terminal
- resultado de:

```bash
launchctl list | grep com.gymsystem.edge.zkteco
```

- resultado de:

```bash
tail -n 100 dist/runtime/edge_zkteco.log
```

- resultado de:

```bash
tail -n 100 dist/runtime/ngrok.stdout.log
```

- URL publica actual de `ngrok` si aplica
- sucursal donde esta instalado
- IP del terminal ZKTeco

## Nota final

Una vez instalado, no hace falta abrir el ejecutable manualmente.

La Mac levantara el edge como servicio al iniciar sesion. Si tambien instalas `ngrok`
en `launchd`, ambos arrancaran en segundo plano y podras reiniciarlos con
`./macos/restart_launchd.sh`.
