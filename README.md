# Edge Service ZKTeco

Servicio edge independiente para terminales ZKTeco (SpeedFace-V5L y equipos similares) del Gym System.

## Instalacion rapida

Este repo contiene solo el edge, para clonarlo en la PC que va junto al lector:

```bash
# Windows (CMD)
git clone https://github.com/miguelsd19/gym-edge-zkteco.git C:\edge_service_zkteco
# macOS
git clone https://github.com/miguelsd19/gym-edge-zkteco.git /Users/Shared/edge_service_zkteco
```

El repo es privado: la PC necesita una cuenta de GitHub con acceso (ver la guia de instalacion).
El `.env` **no** esta en el repo porque lleva el `DEVICE_TOKEN` del punto de acceso: se crea en la
PC a partir de `.env.example` o se copia por un medio seguro. Para actualizar despues: `git pull`
y volver a construir el ejecutable.

Guias paso a paso: [Windows](GUIA_INSTALACION_WINDOWS.md), [macOS](GUIA_INSTALACION_MAC.md) y
[respaldo del lector antes de migrar desde ProGym](GUIA_RESPALDO_PROGYM.md).

**Si la instalacion la va a hacer un agente de IA**, que lea primero [AGENTS.md](AGENTS.md)
(Claude Code lo carga solo desde `CLAUDE.md`): tiene el contexto del sistema, los datos verificados
del lector de Norias, lo que no se debe hacer nunca y el orden del dia del cambio.

El backend vive en el repo principal (`miguelsd19/gym-System`); el edge solo habla con su API.

Es hermano del edge de Hikvision (`edge_service_hikvision/` en el repo principal); queda separado porque ZKTeco usa otro protocolo: el edge se conecta
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
ACCESS_MODE=rfid
ZK_DEVICE_HOST=192.168.1.201
ZK_DEVICE_PORT=4370
ZK_DEVICE_PASSWORD=0
ZK_OPEN_DOOR_ON_GRANT=true
ZK_ALLOWED_GROUP_ID=1
ZK_BLOCKED_GROUP_ID=99
```

Con `ACCESS_MODE=rfid` (recomendado) el `User ID` del lector es el **ID biometrico** que muestra la
ficha del miembro en la app (`rfid_code`), y el edge lo envia a `/api/access/validate/`. Con
`ACCESS_MODE=biometric` se busca en `device_user_id`, que la app no muestra ni deja editar.

## Bloqueo de miembros vencidos (sync de grupos y deshabilitado)

En Hikvision el edge usa `userType=blackList`. En ZKTeco no existe blackList por usuario, asi que
el edge ajusta dos cosas del usuario en el terminal:

- miembro activo con suscripcion vigente → `ZK_ALLOWED_GROUP_ID` (default `1`) y **habilitado**
- miembro inactivo, suspendido o sin suscripcion → `ZK_BLOCKED_GROUP_ID` (default `99`) y
  **deshabilitado** (bit 0 de `privilege`; los bits de rol de admin no se tocan)

El deshabilitado basta para que el terminal niegue el acceso aunque reconozca la cara o huella.
Ademas es como ProGym bloquea a sus vencidos: al migrar una sucursal desde ProGym, el primer sync
habilita a todo miembro activo que ProGym habia dejado deshabilitado.

**Recomendado en el terminal**: crea tambien el grupo `99` (menu Access Control →
Access Groups) **sin timezone de acceso valida**, como segunda barrera.
El grupo `1` es el grupo normal por defecto de ZKTeco.

El sync **no borra usuarios ni enrolamientos**: usa `set_user` re-enviando el registro completo
leido del equipo, cambiando solo el grupo y el bit de deshabilitado. Los templates de cara/huella viven aparte (por `uid`)
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

- `ZK user access updated...`
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

## Respaldo del lector (migracion desde otro sistema)

`backup_terminal` descarga del lector, en modo solo lectura, usuarios, huellas e historial de
marcas (las caras no son descargables y se quedan en el equipo). Paso a paso para la sucursal
que viene de ProGym: [GUIA_RESPALDO_PROGYM.md](GUIA_RESPALDO_PROGYM.md).

```bash
python3 -m edge_service_zkteco.backup_terminal --host 192.168.1.201 --out backups/sucursal
```

## Registro remoto de huella (apagado por defecto)

Con `ZK_REMOTE_ENROLL_ENABLED=true` el backend puede dar de alta a un miembro en el lector y pedir
su huella (flag `zkteco_remote_enroll` del backend). Mismo contrato que el edge Hikvision:

- `POST EDGE_ENROLL_PATH` (`/enroll`) con `{member_id, biometrics, finger_index}` devuelve un
  `job_id`; `POST EDGE_ENROLL_STATUS_PATH` (`/enroll/status`, `{job_id}`) da el estado:
  `preparing`, `creating_user`, `waiting_finger`, `syncing`, `done` o `failed`.
- `biometrics = []` solo da de alta al usuario (al inscribir; en planes de pareja, a cada persona
  una tras otra). `["fingerprint"]` pone el lector en modo enrolamiento (`CMD_STARTENROLL`): la
  persona pone el mismo dedo 3 veces. `["face"]` usa el mismo comando con el índice 111 (como
  `StartEnrollEx` del SDK; pyzk no lo trae) y se confirma con el conteo de caras del lector
  (RL-401 si nadie se puso enfrente, RL-402 si no se guardó). Con ambos, primero huella y luego cara.
- Si el usuario no existe en el lector, se crea con un UID libre en el grupo bloqueado
  (`ZK_BLOCKED_GROUP_ID`); al final se corre el sync de ese miembro (grupo correcto).
- Los errores llevan código RL-xxx y detalle (`error_code`, `error_detail`; ver
  `docs/CODIGOS_ERROR_LECTOR.md` en gym-System). Un sync fallido al final es un aviso (`RL-501`).

Un solo registro a la vez. Mientras el lector espera el dedo (hasta `ZK_ENROLL_TIMEOUT_SECONDS`) el
edge no recibe entradas en vivo; el lector sigue abriendo solo. Nunca toca administradores del
lector ni `ZK_PROTECTED_USER_IDS`.

**Antes de encenderlo, probar que el firmware lo acepta** (con el servicio detenido y alguien junto
al lector):

```bash
sc stop GymEdgeZkteco
.venv\Scripts\python -m edge_service_zkteco.enroll_probe --host 192.168.1.201 --modo ambas
sc start GymEdgeZkteco
```

En Windows, `windows\probar_registro_huella.bat` hace esos tres pasos, guarda la salida en
`pruebas\` y siempre vuelve a arrancar el servicio. Guía para el agente de la PC:
`PRUEBA_REGISTRO_HUELLA.md`.

Crea el usuario de prueba `990001` (bloqueado), pide el dedo, revisa que la huella se guardo y lo
borra. Si el ID ya existe no hace nada. Salida `0` = soportado; `2` = el lector no guardo la huella
(repetir con `--verbose` y guardar la salida para diagnostico).

## Ejecucion

Desde la raiz de este repo:

```bash
pip install pyzk==0.9
python3 -m edge_service_zkteco.main
```

## Tests

```bash
python3 -m unittest discover -s tests
```

La suite corre sin `pyzk` instalado (el cliente lo importa de forma lazy y los tests usan fakes).

## Windows

1. En una PC Windows clona este repo (ver arriba) y crea un entorno virtual en su raiz.
2. Instala las dependencias: `.venv\Scripts\pip install pyzk==0.9 pyinstaller`.
3. Ejecuta `windows\build_windows.bat`.
4. Copia `dist\gym-edge-service-zkteco.exe` junto con tu `.env`.
5. Ejecutalo manualmente una vez para validar red, logs y apertura.
6. Cuando funcione, instalalo como servicio con `windows\install_with_nssm.bat`.

```bat
cd /d C:\edge_service_zkteco
py -3.11 -m venv .venv
.venv\Scripts\pip install pyzk==0.9 pyinstaller
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

1. En una Mac clona este repo (ver arriba) y crea un entorno virtual en su raiz.
2. Instala las dependencias: `pip install pyzk==0.9 pyinstaller`.
3. Ejecuta `macos/build_macos.sh`.
4. Copia `dist/gym-edge-service-zkteco` junto con tu `.env`.
5. Ejecutalo manualmente una vez para validar red, logs y apertura.
6. Cuando funcione, instalalo como servicio con `macos/install_launchd.sh`.

```bash
cd /Users/Shared/edge_service_zkteco
python3 -m venv .venv
source .venv/bin/activate
pip install pyzk==0.9 pyinstaller
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
