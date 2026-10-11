# Guia Para Respaldar El Lector ZKTeco Antes De Dejar ProGym

Esta guia sirve para sacar toda la informacion de la sucursal que hoy usa ProGym Cloud con un lector ZKTeco SpeedFace, antes de conectarlo a Gym System.

Hay que sacar datos de **dos lugares**:

1. **El lector**: usuarios (con el ID que ProGym les asigno), huellas e historial de marcas. Se saca con la herramienta `backup_terminal` de esta carpeta.
2. **ProGym Cloud**: socios, telefonos, correos, planes y fechas de vencimiento. Eso no esta en el lector; se exporta desde ProGym.

**Importante:** las caras del SpeedFace no se pueden descargar. Se quedan guardadas en el lector, asi que **no lo borres, no lo reinicies de fabrica y no elimines usuarios** desde el menu del equipo.

## Que necesitas

- Una computadora (Mac o Windows) conectada a la misma red que el lector.
- Esta carpeta `edge_service_zkteco`.
- Python 3.9 o mas nuevo.
- Acceso de administrador al menu del lector.
- Tu usuario de ProGym Cloud con permisos de administrador.

## Paso 1. Anotar la configuracion del lector

En el SpeedFace entra a **Menu > COMM (Comunicacion)** y anota:

- **Ethernet**: la **IP** del lector (por ejemplo `192.168.1.201`).
- **Conexion a PC (PC Connection)**: el **puerto** (normalmente `4370`) y la **clave de comunicacion (Comm Key)**. Si esta en `0` es que no tiene.
- **Configuracion de servidor en la nube (Cloud Server Setting / ADMS)**: si tiene una direccion configurada, es probable que asi se conecte ProGym. **Solo anotala, todavia no la cambies.**

Toma fotos de esas pantallas: sirven si hay que regresar a ProGym.

## Paso 2. Preparar Python (solo la primera vez)

En Mac abre Terminal; en Windows abre PowerShell. Entra a la carpeta:

```bash
cd ruta/a/edge_service_zkteco
```

Crea un entorno e instala `pyzk`:

Mac:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install pyzk
```

Windows:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install pyzk
```

Si Windows no reconoce `python`, instalalo desde python.org y marca la casilla **Add Python to PATH**.

## Paso 3. Sacar el respaldo del lector

Haz esto en un horario sin gente entrando. Cambia la IP por la del Paso 1:

```bash
python -m edge_service_zkteco.backup_terminal --host 192.168.1.201 --out backups/sucursal-zkteco
```

Si el lector tiene clave de comunicacion, agrega `--comm-key 123456` (el numero del Paso 1).

Vas a ver algo asi:

```
Conectado.
Equipo: SpeedFace-V5L | serie ABC123 | firmware Ver 6.60
Usuarios: 412
Huellas: 380
Historial de marcas: 25000
[AVISO] El equipo tiene 405 caras registradas que no se pueden descargar: se quedan en el lector...
Respaldo completo en backups/sucursal-zkteco
```

La herramienta **solo lee**: no cambia nada en el lector.

### Si no conecta

- Revisa la IP y que la computadora este en la misma red (prueba `ping 192.168.1.201`).
- El lector solo acepta **una conexion a la vez**. Si ProGym tiene un programa o agente instalado en una PC del gimnasio, cierralo mientras haces el respaldo.
- Si sigue fallando, prueba agregar `--timeout 30`, y despues `--force-udp`.

### Si dice "Respaldo INCOMPLETO"

Vuelve a correr el mismo comando. Si el historial de marcas es muy grande y es lo que falla, agrega `--skip-attendance`: el historial es lo menos importante.

## Paso 4. Revisar el respaldo

En la carpeta `backups/sucursal-zkteco` deben quedar:

- `users.csv`: abrelo con Excel. Es la lista de personas registradas en el lector, con su `user_id`, nombre, tarjeta, grupo y cuantas huellas tiene.
- `users.json`, `fingerprints.json`, `attendance.json`: los datos completos. No los edites.
- `device_info.json`: modelo, serie, firmware y cuantas caras, huellas y usuarios tiene el equipo.
- `manifest.json`: resumen y avisos.

Revisa en `manifest.json` la seccion `warnings`:

- **user_id repetidos**: dos personas con el mismo ID en el lector. Hay que decidir cual es la correcta antes de ligarlos a miembros.
- **Usuarios sin user_id**: no se pueden ligar a un miembro; habra que darles un ID.

Copia la carpeta a una USB o a la nube. **Ese respaldo contiene datos personales y biometricos: no lo compartas.**

## Paso 5. Exportar los socios desde ProGym Cloud

Entra a ProGym Cloud y busca en **Socios / Clientes** o en **Reportes** una opcion para exportar a Excel o CSV. Necesitamos, por cada socio:

- El **ID o numero de socio** (debe coincidir con el `user_id` del `users.csv` del lector; compara 3 o 4 personas para confirmarlo).
- Nombre y apellidos.
- Telefono y correo.
- Plan o membresia actual.
- **Fecha de inicio y fecha de vencimiento** de la membresia.
- Estado (activo, vencido o congelado).

Si no encuentras como exportar, pidelo a soporte de ProGym (contacto@progymcloud.com o su WhatsApp): "necesito exportar mi base de socios con sus membresias y fechas de vencimiento en Excel". Haz esto **antes de cancelar la suscripcion de ProGym**, porque despues puedes perder el acceso a esos datos.

Si quieres conservar los pagos historicos, pide tambien el reporte de pagos o ingresos.

## Paso 6. No cambiar nada todavia

Con el respaldo del lector y el Excel de ProGym ya tenemos todo. **No cambies todavia la configuracion de nube del lector** ni desinstales nada de ProGym: eso se hace el dia del cambio, despues de cargar los socios en Gym System. Asi la sucursal sigue operando mientras tanto.

Lo que sigue (cargar los socios en Gym System cruzando `users.csv` con el Excel de ProGym, apagar la conexion con ProGym e instalar el edge) se hace con la guia de instalacion de este servicio.
