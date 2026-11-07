# Prueba del registro remoto de huella y cara (instrucciones para el agente de la PC)

Este archivo es para el agente de IA que opera la PC del edge en **Norias**. Léelo completo junto
con `AGENTS.md` antes de ejecutar nada. Las reglas de `AGENTS.md` siguen aplicando.

## Objetivo

Saber si el SpeedFace-V5L de Norias (firmware `Ver 6.60 Aug 19 2021`) acepta el **registro remoto
de huella**: que el sistema le diga al lector "pide el dedo de este usuario" y el lector lo guarde.
De eso depende el botón "Registrar huella" de la app. El código ya está en `main`, pero apagado
(`ZK_REMOTE_ENROLL_ENABLED=false` en el edge y flag `zkteco_remote_enroll` apagado en el
backend).

**Esta tarea es solo la prueba y el inventario de huellas (paso 5).** No enciendas `ZK_REMOTE_ENROLL_ENABLED`, no reconstruyas el
`.exe` y no cambies el `.env`. Al final reporta el resultado.

## Qué hace la prueba

`windows\probar_registro_huella.bat` hace todo esto:

1. Detiene el servicio `GymEdgeZkteco`, porque el lector acepta una sola conexión. Mientras dura
   la prueba (~2 min), las entradas no llegan al sistema, aunque el lector sigue abriendo solo.
2. Corre `python -m edge_service_zkteco.enroll_probe --host 192.168.1.201 --modo ambas --verbose`, que:
   - crea el usuario de prueba **990001** ("PRUEBA HUELLA") en el grupo bloqueado 99, así que no
     abre el torniquete;
   - pone el lector en modo "ponga su dedo";
   - revisa si la huella quedó guardada;
   - después pone el lector en modo **registro de cara** y revisa que el lector tenga una cara más
     (la cara se pide con el comando del SDK de ZKTeco; con este firmware no está confirmado que
     funcione, por eso se prueba);
   - **borra al usuario de prueba**. Si el ID 990001 ya existe, se detiene sin tocar nada.
3. Guarda la salida en `pruebas\registro-huella-AAAAMMDD-HHMM.log`.
4. **Siempre** vuelve a arrancar `GymEdgeZkteco`, aunque la prueba falle.

No toca a ningún otro usuario, huella ni cara.

## Pasos

### 1. Preparar (solo lectura, sin preguntar)

En CMD **como administrador**:

```bat
cd /d C:\edge_service_zkteco
git status
git branch --show-current
git pull
.venv\Scripts\python -m unittest discover -s tests
dir windows\probar_registro_huella.bat
```

- Si `git status` muestra cambios locales, **detente** y repórtalo. No los descartes.
- Los tests deben terminar en `OK`. Si fallan, detente y reporta la salida.
- Debe estar en la rama `main`. Si no, detente y repórtalo.
- Si `windows\probar_registro_huella.bat` no existe después del `git pull`, el cambio todavía no
  llegó a `main`. Detente y repórtalo.
- `git pull` solo actualiza el código fuente. El servicio corre el `.exe` ya construido en
  `dist\`, así que sigue igual. La prueba usa el código fuente con `.venv`, no el `.exe`.

Verifica la red:

```bat
powershell -Command "Test-NetConnection 192.168.1.201 -Port 4370"
```

### 2. Confirmar con la persona (obligatorio)

Antes de correr la prueba, explícale y pide un "sí" explícito:

> "Voy a detener el servicio del lector unos 2 minutos para probar el registro remoto de huella.
> Se crea un usuario de prueba (ID 990001) que no abre el torniquete, y al final se borra. Necesito
> que alguien esté junto al lector para poner el mismo dedo 3 veces cuando lo pida. Mejor con
> poca gente entrando. ¿Lo hago?"

No continúes sin esa confirmación.

### 3. Correr la prueba

```bat
windows\probar_registro_huella.bat
```

Cuando la consola diga **"Ve al lector AHORA"**, avisa a la persona. Si el lector cambia de
pantalla y pide la huella, que ponga el **mismo dedo 3 veces** (por defecto, el índice derecho).
Después la consola dice **"Ponte FRENTE al lector AHORA"**: que la misma persona se ponga de frente
al lector hasta que termine (hasta ~1 minuto). Para probar solo una cosa:
`windows\probar_registro_huella.bat 192.168.1.201 huella` o `... cara`.

### 4. Verificar que todo quedó como antes

```bat
sc query GymEdgeZkteco
powershell -Command "Get-Content dist\runtime\edge_zkteco.log -Tail 30"
```

- El servicio debe estar en `RUNNING` y el log debe mostrar `Connected to ZKTeco device` después
  de la prueba. Si no arrancó: `sc start GymEdgeZkteco`.
- Revisa la salida de la prueba: debe decir `Usuario de prueba 990001 borrado del lector`. Si dice
  `[AVISO] No se pudo borrar`, pide a la persona borrarlo en el lector (Menú > Usuarios > 990001).
  Es el **único** usuario que se puede borrar a mano, porque es el de prueba.
- Pide a alguien que pase por el lector y confirma que en el log aparecen `Candidate ZK event
  received` y `Access result: granted=...` de esa marca.

## 5. Inventario de huellas (después de la prueba)

Sirve para que la app sepa qué miembros **ya tienen huella** en el lector (los registrados antes
del registro remoto). Es de **solo lectura**: no escribe nada en el lector y el archivo no lleva
plantillas ni nombres, solo `user_id` y cuántas huellas tiene cada uno. Las caras no se pueden leer
por usuario; solo sale el total.

También detiene el servicio (~1-2 min), así que pide confirmación igual que en el paso 2:

> "Voy a detener otra vez el servicio del lector 1 o 2 minutos para leer qué socios ya tienen
> huella. No cambia nada en el lector. ¿Lo hago?"

```bat
windows\inventario_huellas.bat
```

- `RESULTADO 0`: se guardó `pruebas\huellas-AAAAMMDD-HHMM.json`. Revisa con
  `sc query GymEdgeZkteco` que el servicio quedó `RUNNING`.
- `RESULTADO 1` o `2`: repítelo una vez; si sigue, repórtalo con la salida.

**No subas el archivo a ningún lado ni lo cargues al sistema.** Entrégaselo a la persona (por
ejemplo, cópialo a una USB o pásale su contenido) y reporta la línea
`Usuarios: N | con huella: N | sin huella: N`. La carga a Gym System la hace el dueño después de
revisar la lista.

## Cómo interpretar el resultado

| Código | Significado | Qué reportar |
|---|---|---|
| `0` | El lector **sí** acepta el registro remoto. | "Funciona" y la parte de la salida con `[RESULTADO]`. |
| `2` | El lector no guardó la huella o la cara (la línea `[RESULTADO]` dice cuál): rechazó el comando, no la pidió o se agotó el tiempo. | Qué se vio en la pantalla del lector en cada paso y **el log completo** de `pruebas\` (incluye los paquetes del registro de cara). |
| `1` | No conectó al lector. | Si el servicio seguía corriendo y el resultado de `Test-NetConnection`. |
| `3` | El ID 990001 ya existe en el lector; no se tocó nada. | Eso. Se puede repetir con otro ID libre: `.venv\Scripts\python -m edge_service_zkteco.enroll_probe --host 192.168.1.201 --verbose --user-id 990002` (con el servicio detenido). |
| `4` | Error inesperado. | El log completo. |

Si el código es `2`, también es útil saber si el lector **mostró la pantalla de "ponga su dedo"**:

- Si la mostró pero no guardó: el comando sí llega y falla después.
- Si no la mostró: el firmware no acepta el comando.

## Qué reportar al final

1. Código de salida y la línea `[RESULTADO]`.
2. Qué se vio en la pantalla del lector.
3. Que el servicio quedó `RUNNING` y que entra la marca de prueba.
4. Que el usuario 990001 se borró.
5. El resumen del inventario de huellas y dónde quedó el archivo `pruebas\huellas-*.json`.
6. El archivo de `pruebas\` si el resultado no fue `0`. Lleva la salida del protocolo, pero no
   datos de socios.

Si salió `0`, el siguiente paso **no lo hace este agente sin instrucción del dueño**: reconstruir el
`.exe`, poner `ZK_REMOTE_ENROLL_ENABLED=true` en `dist\.env` y encender el flag
`zkteco_remote_enroll` de Norias en el backend.
