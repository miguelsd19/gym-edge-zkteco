# Entradas durante la sincronizacion (SpeedFace 6.60, pyzk 0.9)

El hilo del lector usa el mismo socket para comandos y ventanas de eventos.
No usa `pyzk.live_capture()`: esa funcion descarga todos los usuarios antes
de cada ventana, incluso si la siguiente orden la interrumpe inmediatamente.

La captura propia conserva las operaciones de inicio/cierre de pyzk, recibe
tramas TCP completas (incluida fragmentacion y varias tramas en un recv), acusa
recibo y decodifica los formatos ATTLOG de 12/32/36/52 bytes. Usa el user_id
del evento; no inventa el UID interno cuando no viene en el paquete.
Los formatos desconocidos se registran y no se envian al backend.
Al cerrar, restaura el timeout, desuscribe con reg_event(0) y restaura el
estado deshabilitado del equipo si estaba deshabilitado al entrar.

- `ZK_COMMAND_BATCH_SIZE=4`: limite de comandos antes de volver a escuchar.
- `ZK_CAPTURE_MIN_WINDOW_SECONDS=0.5`: escucha minima aunque haya ordenes en cola.
- `ZK_CAPTURE_POLL_SECONDS=1.0`: limite de una espera del socket; durante la
  ventana minima se reduce para no extender innecesariamente esa ventana.

No se requiere modificar un .env existente: los valores anteriores son defaults.
La lectura get_users que necesita la sincronizacion sigue siendo explicita;
no se repite por abrir la captura. Los comandos largos pueden ocupar el socket
durante su ejecucion; no se promete capturar mientras se transfiere una respuesta.

## Diagnostico fisico del 2026-10-31

Con el servicio detenido y una sola conexion pyzk, descargar 1,462 usuarios
tardo 0.812 s. La captura cruda se suscribio a EF_ATTLOG | EF_VERIFY | EF_ALARM
(641). Una entrada vigente produjo comando 500, con user_id y ATTLOG de 36 bytes.
La prueba del usuario bloqueado en grupo 99 no produjo otro paquete. Tampoco
lo produjo una cara no registrada. La persona presente confirmo apertura para
el vigente y rechazo fisico para los otros dos casos.

El coste repetido de live_capture queda confirmado por el codigo de pyzk y la
medicion. La prueba del vigente con el sync en curso debe confirmar el resultado
del despliegue. No se interpreta un rechazo sin un formato observado e identificado:
la prioridad de este cambio es entregar entradas permitidas durante el sync.
Los paquetes crudos permanecen fuera del repositorio en la PC del gimnasio.
