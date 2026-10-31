# Contexto para agentes de IA

Este archivo es para un agente de IA (Claude Code, Codex, etc.) que vaya a instalar, operar o
diagnosticar el edge en la PC de una sucursal. Léelo completo antes de ejecutar nada.

## Qué es esto

- **Gym System** es el sistema de gestión del gimnasio (repo principal privado
  `miguelsd19/gym-System`: backend Django en Railway, frontend React). Tiene varias sucursales.
- **Este repo** es solo el **edge**: un servicio Python que corre en una PC de la sucursal, junto
  al lector facial/huella **ZKTeco SpeedFace**. Hace de puente entre el lector y el backend:
  1. Escucha los eventos del lector por TCP `4370` (librería `pyzk`).
  2. Pregunta al backend si la persona puede pasar (`POST /api/access/validate/` con su
     `device_token`) y, si sí, abre el torniquete.
  3. Sincroniza en el lector quién está bloqueado: miembro vencido → grupo `99` y
     **deshabilitado**; miembro activo → grupo `1` y **habilitado**.
  4. Expone un servidor local en `9877` para que el backend pida abrir la puerta o sincronizar
     (vía `ngrok` si el backend está en la nube).
- La otra sucursal (Calvillo) usa un lector Hikvision con otro edge (`edge_service_hikvision/` en
  el repo principal) y, desde el 2026-10-29, **otra instancia del backend** (se traspasó). Norias
  y Calvillo ya no comparten backend: este edge solo habla con el de Norias. No toques Calvillo.

## Sucursal Norias (primera instalación de este edge)

Datos verificados el 2026-10-11/15:

| Dato | Valor |
|---|---|
| Lector | SpeedFace-V5L, serie `SYZ8234900145`, firmware `Ver 6.60 Aug 19 2021` |
| Red del lector | IP fija `192.168.1.201`, puerto `4370`, **sin comm key** (`ZK_DEVICE_PASSWORD=0`) |
| Red del gimnasio | `192.168.1.x`, gateway `192.168.1.1` |
| Backend | `https://sh-fitness-norias-api.up.railway.app` (instancia propia de Norias, proyecto Railway `SH-Fitness-Norias`) |
| Sucursal en el backend | slug `norias`, punto de acceso **"Entrada Norias"** |
| Modo | `ACCESS_MODE=rfid` |
| Contenido del lector | 1,412 usuarios, 1,347 huellas, 29 caras (las caras **no** se pueden descargar) |

Historia que explica el estado del lector:

- La sucursal venía de **ProGym Cloud** (otro software). En el lector, el `User ID` de cada
  persona **es su ID de cliente en ProGym**. Al migrar, cada miembro de Norias en Gym System
  quedó con ese mismo número como "ID biométrico" (`rfid_code` y `device_user_id`). Los nuevos
  miembros dados de alta en Gym System reciben IDs numéricos consecutivos (después del 3792).
- **ProGym bloqueaba a los vencidos deshabilitándolos** (bit 0 de `privilege` en el protocolo
  ZK). Por eso al inicio había 935 usuarios deshabilitados. El sync de este edge habilita o
  deshabilita según el backend, así que corrige ese estado en la primera sincronización.
- Los usuarios del lector con IDs **7001–7026** son personal o familiares y los IDs **98989** y
  **99999** son administradores de ProGym. **No son miembros en Gym System y se dejan como
  están** (decisión del dueño). El sync no los toca porque no están en el backend.
- La primera sincronización actualizará a casi todos los miembros ligados (hoy ninguno tiene
  grupo asignado en el lector). Es esperado.

## Reglas que no se rompen

1. **Nunca borres usuarios del lector a mano, ni lo reinicies de fábrica, ni "limpies datos".** Las
   caras no se pueden respaldar: si se pierden, hay que reenrolar a cada persona. La única excepción
   es el borrado que pide el backend (comando `purge_inactive_members`, socios con más de 6 meses
   sin asistir) por la ruta `EDGE_DELETE_USERS_PATH`. Aun así, el edge nunca borra administradores
   del lector ni los IDs de `ZK_PROTECTED_USER_IDS` (en Norias: `7001-7026,98989,99999`).
2. **El lector acepta una sola conexión a la vez.** Antes de arrancar el edge (o la herramienta de
   respaldo), ProGym u otro programa no debe estar conectado. Si la conexión falla aunque el lector
   responde en `4370`, sospecha esto primero.
3. **El `.env` lleva el `DEVICE_TOKEN` (una credencial).** Nunca lo imprimas completo en la consola,
   nunca lo subas a git (ya está en `.gitignore`) y nunca lo pegues en chats o logs. Para
   verificar que existe, muestra solo los nombres de las variables o los primeros caracteres.
4. **Los respaldos del lector (`backups/`) tienen datos personales y biométricos.** No salen de la
   PC salvo a una USB o nube que indique la persona. Nunca a git.
5. **No cambies nada en el backend ni en producción desde esta PC.** Si algo del backend está mal
   (token rechazado, miembros faltantes), detente y repórtalo a la persona.

## Qué puedes hacer sin preguntar y qué debes confirmar

**Sin preguntar (solo lectura o local):**
- Verificar red: `powershell -Command "Test-NetConnection 192.168.1.201 -Port 4370"`.
- Instalar Git, Python 3.11, NSSM y ngrok; clonar; crear `.venv`; construir el `.exe`.
- Correr los tests: `.venv\Scripts\python -m unittest discover -s tests`.
- Sacar un respaldo del lector (**no escribe nada en él**):
  `.venv\Scripts\python -m edge_service_zkteco.backup_terminal --host 192.168.1.201 --out backups\norias-AAAA-MM-DD`
- Leer logs (`dist\runtime\edge_zkteco.log`) y el estado de los servicios (`sc query ...`).

**Confirma con la persona antes de:**
- **Arrancar el edge contra el lector real** (manual o como servicio): al arrancar sincroniza
  roles y **escribe en el lector** (grupos y habilitado/deshabilitado). Solo se hace el día del
  cambio, con ProGym ya apagado y la importación de miembros al día (ver abajo).
- Abrir la puerta de prueba, instalar o reiniciar servicios, cambiar cualquier configuración del
  lector (red, comm key, nube/ADMS, grupos).
- Cualquier cosa que no esté en esta guía o en `GUIA_INSTALACION_WINDOWS.md`.

## Orden del día del cambio (Norias)

1. **Respaldo nuevo del lector** con `backup_terminal` (después del 11 de octubre se inscribieron
   13 clientes nuevos en ProGym). Compara el número de usuarios con el respaldo anterior y avisa
   a la persona.
2. **Importar el export más reciente de ProGym** en Gym System. **No se hace desde esta PC**: lo
   corre el responsable del sistema con el comando `import_progym` del repo principal. Espera su
   confirmación antes de seguir.
3. **Apagar ProGym**: cerrar su programa o agente en la PC del gimnasio y, en el lector, quitar la
   configuración de servidor en la nube (`COMM → Cloud Server Setting / ADMS`) si apunta a ProGym.
   Esto lo hace o lo autoriza la persona.
4. **Instalar y arrancar el edge** siguiendo `GUIA_INSTALACION_WINDOWS.md` (pasos 1 al 8).
5. **Verificar**:
   - En el log: `Connected to ZKTeco device 192.168.1.201:4370` y `ZK user role sync completed`.
   - Un **miembro vigente** pasa y la puerta abre.
   - Un **miembro vencido** es rechazado (el lector lo reconoce pero no abre).
   - El personal (IDs 7001–7026) sigue pasando como antes.
   - Reporta a la persona los contadores del sync (`updated`, `missing`, `skipped`).

## Cómo verificar el .env sin exponer el token

```bat
findstr /B "BACKEND_BASE_URL ACCESS_MODE ZK_DEVICE_HOST ZK_DEVICE_PORT ZK_DEVICE_PASSWORD" dist\.env
powershell -Command "(Get-Content dist\.env | Select-String '^DEVICE_TOKEN=').Line.Length"
```

El segundo comando debe dar 61 (`DEVICE_TOKEN=` + 48 caracteres). Valores esperados en Norias:
`ACCESS_MODE=rfid`, `ZK_DEVICE_HOST=192.168.1.201`, `ZK_DEVICE_PORT=4370`, `ZK_DEVICE_PASSWORD=0`.

## Diagnóstico rápido

| Síntoma en el log o en la consola | Causa probable |
|---|---|
| `Could not connect to ZKTeco device` repetido | Red, IP, comm key, o **otro programa conectado al lector** |
| `HTTP 401 ... Valid device token required` | `DEVICE_TOKEN` incorrecto o el punto de acceso se desactivó: repórtalo, no lo "arregles" |
| El sync reporta muchos `missing` | Miembros sin ID biométrico en el backend o personas que no están en el lector: reporta la lista |
| Un miembro vigente no pasa | Revisa en la app que su ID biométrico coincida con su `User ID` en el lector y que su membresía esté vigente |
| El túnel ngrok no arranca como servicio | Falta `NGROK_AUTHTOKEN` en el servicio (ver paso 8 de la guía) |

## Para desarrollar en este repo

- Python ≥ 3.9, solo stdlib más `pyzk` (import lazy). Tests: `python -m unittest discover -s tests`
  (corren sin `pyzk` ni hardware; usan fakes).
- `ZK_EVENT_SOURCE=file` permite probar sin lector (lee IDs de `runtime/input.txt`).
- La misma carpeta existe en el repo principal (`edge_service_zkteco/`). Por ahora se mantienen las
  dos copias: si cambias código aquí, avisa para replicarlo allá.
