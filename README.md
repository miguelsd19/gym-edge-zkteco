# Edge Service ZKTeco

Servicio edge independiente para terminales ZKTeco (SpeedFace-V5L y equipos similares) del Gym System.

Queda separado de `edge_service_hikvision/` porque ZKTeco usa otro protocolo: el edge se conecta
al terminal por TCP al puerto `4370` usando la libreria `pyzk`, recibe los eventos de
reconocimiento en vivo y ejecuta la apertura remota por el mismo canal.

## Que hace

- se conecta al terminal ZKTeco por `pyzk` (puerto 4370) y captura eventos en vivo
- extrae el `user_id` del evento (el ID de usuario enrolado en el terminal)
- valida el acceso contra `POST /api/access/biometric-events/` con `Bearer <device_token>`
- si el backend concede, manda `unlock` al terminal (configurable con `ZK_OPEN_DOOR_ON_GRANT`)
- reporta salud del edge al backend (`POST /api/access/edge-health/`)
- sincroniza miembros vencidos al terminal moviendolos a un grupo bloqueado (ver abajo)
- expone un servidor de control local para apertura remota y sync bajo demanda desde el backend
- guarda logs, eventos crudos e intentos fallidos para soporte
- recuerda eventos ya procesados para no repetir aperturas
- watchdog + auto-restart si pierde comunicacion con el terminal

## Flujo

1. El SpeedFace reconoce una cara/tarjeta y emite un evento en vivo.
2. El edge extrae el `user_id` y llama al backend con `Bearer <device_token>`.
3. Si el backend responde `should_open_door=true`, el edge manda `unlock` al terminal.
4. Si el backend no responde, el acceso se niega por regla de seguridad y el intento
   queda en `runtime/failed_attempts_zkteco.jsonl`.

Nota fisica importante: el SpeedFace controla su relay localmente. Un usuario enrolado y con
permisos en el terminal puede abrir la puerta por si solo al reconocerse, sin esperar al backend.
Por eso el bloqueo real de miembros vencidos lo hace la **sincronizacion de grupos** (abajo), y el
`unlock` tras cada grant es redundante-inofensivo en operacion normal.

## Configuracion

1. Copia `.env.example` a `.env`.
2. Ajusta `BACKEND_BASE_URL`, `DEVICE_TOKEN`, `ZK_DEVICE_HOST` y opcionalmente `ZK_DEVICE_PASSWORD` (comm key).
3. Asegura que el `device_user_id` del miembro en Django coincida con el `User ID` enrolado en el terminal.

Ejemplo:

```env
BACKEND_BASE_URL=https://tu-backend
DEVICE_TOKEN=token-del-access-point
ACCESS_MODE=biometric
ZK_DEVICE_HOST=192.168.1.201
ZK_DEVICE_PORT=4370
ZK_DEVICE_PASSWORD=0
ZK_OPEN_DOOR_ON_GRANT=true
ZK_ALLOWED_GROUP_ID=1
ZK_BLOCKED_GROUP_ID=99
```

Si prefieres reutilizar el `rfid_code` del miembro puedes poner `ACCESS_MODE=rfid`; el edge
enviara el identificador a `/api/access/validate/` como `rfid_code`.

## Bloqueo de miembros vencidos (sync de grupos)

En Hikvision el edge usa `userType=blackList`. En ZKTeco no existe blackList por usuario, asi que
el edge mueve a los usuarios entre **grupos de acceso** del terminal:

- miembro activo con suscripcion vigente → `ZK_ALLOWED_GROUP_ID` (default `1`)
- miembro inactivo, suspendido o sin suscripcion → `ZK_BLOCKED_GROUP_ID` (default `99`)

**Configuracion obligatoria en el terminal**: crea el grupo `99` (menu Access Control →
Access Groups) **sin timezone de acceso valida**, para que sus miembros no puedan abrir la puerta.
El grupo `1` es el grupo normal por defecto de ZKTeco.

El sync **no borra usuarios ni enrolamientos**: usa `set_user` re-enviando el registro completo
leido del equipo, cambiando solo el grupo. Los templates de cara/huella viven aparte (por `uid`)
y no se tocan. Aun asi, tras la primera puesta en marcha verifica con un usuario de prueba:

1. Enrola una cara de prueba en el terminal.
2. Fuerza un sync que lo bloquee (o venca su membresia en el backend).
3. El terminal debe seguir **reconociendo** la cara pero **negar** el acceso.
4. Reactiva la membresia y sincroniza: la puerta debe abrir de nuevo.

El sync corre una vez al arrancar el edge, despues cada `ZK_USER_ROLE_SYNC_INTERVAL_SECONDS`
(24 h por default), y bajo demanda cuando el backend lo solicita (p. ej. al renovar una membresia).
Solo toca a los miembros que devuelve el backend; usuarios de staff u otros quedan intactos.
Si un firmware da problemas, se puede apagar con `ZK_USER_ROLE_SYNC_ENABLED=false`.

Busca estos logs:

- `ZK user group updated...`
- `ZK user role sync completed...`

## Apertura remota desde un backend en la nube

Igual que en el edge Hikvision: si el backend vive fuera de la red local (p. ej. Railway),
expone el listener de control `9877` con `ngrok` y guarda la URL publica en
`AccessPoint.edge_control_url`.

```bash
ngrok http 9877
```

- `edge_control_url` = `https://mi-sucursal.ngrok-free.dev` (sin `/door/open`; el backend lo anexa)
- agrega el host a `ACCESS_EDGE_CONTROL_ALLOWED_HOSTS` en el backend
- el edge expone `POST /door/open` y `POST /hikvision/user-role-sync` (mas el alias
  `/zkteco/user-role-sync`), autenticados con el mismo `device_token`

## Modo file (pruebas sin hardware)

Con `ZK_EVENT_SOURCE=file` el edge no se conecta a ningun terminal: lee identificadores de
`runtime/input.txt` (una linea = un `device_user_id`, o un JSON con `user_id`/`timestamp`).
La apertura es un no-op logueado. Sirve para probar el flujo completo contra el backend real:

```bash
echo "1001" >> runtime/input.txt
```

## Ejecucion

```bash
cd edge_service_zkteco
pip install pyzk
python3 -m edge_service_zkteco.main
```

## Tests

```bash
cd edge_service_zkteco
python3 -m unittest discover -s tests
```

La suite corre sin `pyzk` instalado (el cliente lo importa de forma lazy y los tests usan fakes).

## Windows

1. En una PC Windows crea un entorno virtual dentro de `edge_service_zkteco`.
2. Instala las dependencias: `.venv\Scripts\pip install pyzk pyinstaller`.
3. Ejecuta `windows\build_windows.bat`.
4. Copia `dist\gym-edge-service-zkteco.exe` junto con tu `.env`.
5. Ejecutalo manualmente una vez para validar red, logs y apertura.
6. Cuando funcione, instalalo como servicio con `windows\install_with_nssm.bat`.

```bat
cd edge_service_zkteco
py -3.11 -m venv .venv
.venv\Scripts\pip install pyzk pyinstaller
windows\build_windows.bat
copy .env dist\.env
dist\gym-edge-service-zkteco.exe
```

Notas:

- El `.env` debe vivir junto al `.exe`; `runtime\` se crea junto al `.exe`.
- Para 24/7 usa NSSM (`windows\install_with_nssm.bat`, servicio `GymEdgeZkteco`).
- Para apertura remota desde la nube deja `ngrok` permanente hacia `9877`
  (`windows\install_ngrok_with_nssm.bat`, servicio `GymEdgeZktecoNgrok`).
- `windows\restart_services.bat` reinicia ambos servicios.

## macOS

1. En una Mac crea un entorno virtual dentro de `edge_service_zkteco`.
2. Instala las dependencias: `pip install pyzk pyinstaller`.
3. Ejecuta `macos/build_macos.sh`.
4. Copia `dist/gym-edge-service-zkteco` junto con tu `.env`.
5. Ejecutalo manualmente una vez para validar red, logs y apertura.
6. Cuando funcione, instalalo como servicio con `macos/install_launchd.sh`.

```bash
cd edge_service_zkteco
python3 -m venv .venv
source .venv/bin/activate
pip install pyzk pyinstaller
./macos/build_macos.sh
cp .env dist/.env
./dist/gym-edge-service-zkteco
```

Servicio launchd: `com.gymsystem.edge.zkteco` (`macos/install_launchd.sh` /
`macos/uninstall_launchd.sh`). Ngrok: `com.gymsystem.edge.zkteco.ngrok`
(`macos/install_ngrok_launchd.sh`). `macos/restart_launchd.sh` reinicia ambos.

## Notas operativas

- `ZK_DEVICE_PASSWORD` es la comm key del terminal (0 si no configuraste una).
- `ZK_OMIT_PING=true` evita depender de ICMP para conectar (recomendado).
- `ZK_FORCE_UDP=true` solo si tu terminal no acepta TCP en 4370.
- `ZK_CAPTURE_POLL_SECONDS` acota la latencia de los comandos (apertura remota/sync) mientras
  la captura en vivo esta activa: es el peor caso de espera para interrumpirla.
- `ZK_KEEPALIVE_SECONDS` manda un `get_time` periodico si no hay eventos; mantiene vivo el
  socket y alimenta el reporte de salud.
- `ZK_EVENT_LOG_FILE` guarda cada evento crudo recibido del terminal.
- `ZK_STATE_FILE` deduplica eventos procesados entre reinicios.
- `ZK_ALLOWED_PUNCH_CODES` / `ZK_ALLOWED_STATUS_CODES` filtran eventos por codigo; vacio = todos.
  Dejalos vacios al inicio y revisa `runtime/zk_events.jsonl` para decidir si necesitas filtrar.
- `ZK_RECOVERY_RESTART_SECONDS=300` reinicia el proceso si no hay comunicacion exitosa con el
  terminal en ese lapso (el keepalive lo mantiene fresco en operacion sana).
- El watchdog (`EDGE_WATCHDOG_*`) mata el proceso si el loop principal se cuelga; el supervisor
  (launchd/NSSM) lo relanza.

## Recomendacion de puesta en marcha

1. Crea un `AccessPoint` dedicado para este torniquete y toma su `device_token`.
2. Asigna una IP fija al SpeedFace y verifica alcance al puerto 4370 desde la PC del edge.
3. Configura el grupo bloqueado (`99`) en el terminal sin timezone de acceso.
4. Enrola usuarios usando el mismo `User ID` que guardaras en `Member.device_user_id`.
5. Arranca el servicio y revisa `runtime/zk_events.jsonl` para confirmar que llegan eventos.
6. Valida el ciclo completo: cara → grant → puerta; apertura remota; bloqueo/desbloqueo de un
   usuario de prueba sin perder su enrolamiento.
