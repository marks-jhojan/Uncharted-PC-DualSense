# Uncharted PC DualSense

**Gatillos adaptativos, hápticos y lightbar del DualSense para *Uncharted: The Nathan Drake Collection* en PC.**

*Read in English: [README.md](README.md)*

Uncharted PC DualSense (**UNDS** para abreviar — el nombre que verás dentro del paquete: `UNDS.exe`,
`UNDS Studio`) añade **gatillos adaptativos, hápticos y lightbar en vivo** al port nativo no oficial
[uncharted_pc](https://github.com/Supermedo/uncharted_pc) de SuperMedo. Los juegos de PS4 no traen datos
hápticos (solo el rumble de los dos motores del DualShock 4): todos los efectos se generan a partir de
eventos que UNDS lee del juego en marcha.

Tiene dos partes:

- **Un conjunto pequeño de parches para el port** (`patches/v0.3.9`): el port publica eventos del juego por
  UDP local (rumble, mando, lightbar, niveles de audio y, en Uncharted 1, el estado del juego: vida, arma,
  munición, disparos y daño con su dirección) y escribe las órdenes de gatillos y lightbar en el mando con
  SDL. Sin la variable `UNDS_EVENTS` el port se comporta exactamente igual que el original.
- **El companion** (`UNDS.exe`, fuente en Python en `companion/`): convierte esos eventos en efectos de
  gatillo, colores de lightbar y hápticos (audio WASAPI a los actuadores de bobina del DualSense). El port
  lo arranca con el juego, lo abras como lo abras.

## Qué hace

| | Uncharted 1 (versiones 1.00 y 1.02 del juego) | Uncharted 2 / 3 |
|---|---|---|
| Gatillos adaptativos | por clase de arma: pistola, revólver, subfusil, fusil automático, escopeta, lanzagranadas, granadas, torreta montada (jeep, moto acuática); golpe de retroceso por disparo, cargador vacío, peso al apuntar, calma/combate, menús y cinemáticas | perfil genérico por la firma de rumble de cada disparo |
| Hápticos | rumble del juego resintetizado para los actuadores, una voz por disparo (por clase de arma), golpes con dirección (izquierda/derecha, delante/detrás), muerte | rumble del juego + disparos detectados por el rumble |
| Lightbar | color del juego, tinte según la vida, destellos de daño, fundido al morir | color del juego, estimación de daño |

Los hooks del estado del juego se localizan por patrón de instrucciones al arrancar. Los cuatro aparecen
exactamente una vez en el ejecutable de U1 de las versiones 1.00 y 1.02 (1.00 jugada, 1.02 comprobada con
`tools/check_unds_hooks.py`); si tu ejecutable es distinto simplemente no se activan y UNDS pasa al modo
genérico.

## Requisitos

- Windows 10/11 x64 y la **release oficial de uncharted_pc v0.3.9** funcionando con tu propio dump del juego.
- **DualSense por USB** (los hápticos son audio y solo van por USB; gatillos y lightbar van por SDL y
  deberían funcionar también por Bluetooth, sin probar).
- **DSX cerrado** (apaga los hápticos y se pelea por los gatillos) y Steam Input apagado para el port.
- Unos 11,5 GB de memoria libre (RAM + archivo de paginación), lo que ya necesita el port.

## Instalación

1. Extrae el `uncharted-windows.zip` oficial (v0.3.9) y comprueba que el juego arranca.
2. Extrae `Uncharted-PC-DualSense-1.0.0-uncharted_pc-0.3.9-windows.zip` **en la carpeta que contiene `uncharted-windows`**
   (no dentro de ella): el zip ya trae una carpeta `uncharted-windows` que se mezcla con la tuya
   (sobrescribe cuando lo pregunte). Sustituye `run.py` y `bin\`
   (port + parches UNDS; tus archivos de DLSS/XeSS se quedan) y añade `UNDS\`.
3. Configuración de sonido de Windows → Reproducción → *Altavoces (Wireless Controller)* → Configurar →
   **Cuadrafónico**, y el volumen de ese dispositivo al **100 %** (los canales 3 y 4 son los actuadores).
4. En el launcher del port, *Fixes → Mesh raycaster copies* = **Automatic** (con UNDS queda activo en U1 y
   U2, donde el juego puede detenerse con `iSubMesh` tras un punto de control).
5. **Juega como siempre** (`Uncharted.exe`, el acceso directo del launcher en el escritorio o en Steam). Al
   arrancar el juego, el companion de UNDS arranca con él en una consola minimizada y se cierra al salir
   (`window = hidden` en `unds_launcher.ini`: sin ventana). Su registro es `UNDS\logs\unds.log`.

También puedes abrir `UNDS\UNDS.exe` con doble clic: antes comprueba la memoria libre, un juego anterior
abierto y DSX, y luego abre el launcher del port. Con `auto = no` en `unds_launcher.ini` (o `UNDS_AUTO=0` en
el campo *Environment* del launcher) el juego deja de arrancar UNDS solo.

Si la lightbar **parpadea en rojo** y suena un aviso, el port informó de un problema (arranque defectuoso
que puede perder datos, aviso de pérdida de datos, aserción del juego o caída): la consola dice cuál. Tras
un arranque defectuoso, cierra el juego y vuelve a abrirlo antes de seguir jugando.

## UNDS Studio: calíbralo a tu gusto

![UNDS Studio](docs/studio.png)

`UNDS\UNDS Studio.cmd` (o `UNDS.exe --studio`) abre una página local en el navegador para ajustarlo todo
sintiéndolo, al estilo de DSX, con el juego **cerrado** y el mando por USB:

- **Gatillos en vivo**: L2 y R2 dibujados con sus 10 zonas de resistencia y vibración y la posición de tu dedo.
- **Galería de tiro**: eliges un arma, aprietas R2 en el mando y sientes el disparo igual que en el juego
  (fases del retroceso, voz háptica, ráfagas de las automáticas, cargador; □ recarga).
- **Editores visuales**: efectos de gatillo con deslizadores, el retroceso como línea de tiempo de fases,
  voces hápticas con su forma de onda, una brújula para sentir el daño direccional, la muerte, los motores
  de rumble, los colores de la lightbar y todos los demás parámetros en una tabla *Avanzado*.

Cada cambio se valida y se guarda directamente en los `.ini` (al abrir el Studio se hace una copia;
*Restaurar* la recupera), así que con el juego abierto el companion lo aplica al momento; entonces el
Studio solo edita, porque el juego es dueño del mando.

## Ajustes

Todo en `uncharted-windows\UNDS\`, se recarga al guardar mientras juegas:

- `unds_triggers.ini` — efectos de gatillo por clase de arma y por arma, lightbar, tiempos de combate.
- `unds_haptics.ini` — voces hápticas, traducción del rumble, dispositivo.
- `unds_launcher.ini` — arrancar o no con el juego, oculto o en consola, grabación, comprobaciones y
  variables que se pasan al port.

`record = yes` en `unds_launcher.ini` guarda una traza JSONL de cada partida en `UNDS\recordings\` para
reportar fallos. `UNDS.exe --help` lista las opciones (`--selftest` comprueba las bibliotecas empaquetadas y
los dispositivos de audio, `--check`, `--no-haptics`, `--no-triggers`, `-v`).

## Problemas frecuentes

- **Sin hápticos**: DSX abierto, mando por Bluetooth, el dispositivo no está en Cuadrafónico o tiene el
  volumen bajo, o `UNDS_RUMBLE=0` pisado en el campo *Environment* del launcher.
- **Los gatillos no hacen nada**: el campo *Environment* del launcher gana a las variables de UNDS; quita de
  ahí cualquier `UNDS_EVENTS` (si está puesto ahí, el juego entiende que tú arrancas un companion aparte).
  `UNDS.exe --check` verifica que `bin\bb-probe.exe` trae UNDS.
- **El antivirus avisa de UNDS.exe**: a veces marcan los ejecutables de PyInstaller; puedes usar el
  companion desde el código fuente (abajo).
- **El launcher del port ofrece actualizar**: la actualización sustituye `bin\` y `run.py`; UNDS necesita una
  release compilada para esa versión.

## Compilar desde el código

En una shell MSYS2 CLANG64 (el entorno de compilación del propio port, ver su `packaging/windows/README.md`)
y con Python 3.12 de python.org:

```sh
bash scripts/unds_build.sh --deps   # clona uncharted_pc v0.3.9, aplica patches/v0.3.9, compila y empaqueta
bash scripts/unds_release.sh        # UNDS.exe (PyInstaller) + build/Uncharted-PC-DualSense-<versión>-...zip
```

Companion desde el código (CMD): `pip install -r companion\requirements.txt` y luego
`python companion\unds_launcher.py` junto al port, o `python companion\unds.py` con
`UNDS_EVENTS=127.0.0.1:47111 UNDS_TRIGGER=30,95 UNDS_RUMBLE=0` en el campo *Environment* del launcher.
`tools\check_unds_hooks.py` comprueba los patrones del estado del juego contra un ejecutable.

### Los parches

| | |
|---|---|
| 0001 | bus de eventos (UDP) para companions externos del mando |
| 0002 | `UNDS_LED`, `UNDS_TRIGGER` y recorrido físico de los gatillos en los eventos del mando |
| 0003 | estado del juego en U1 (vida, arma, munición, disparos, daño) |
| 0004 | canal de control: gatillos adaptativos y lightbar por SDL |
| 0005 | red zone: reserva la memoria de thunks antes de la GPU (arregla arranques defectuosos poco frecuentes; candidato upstream) |
| 0006 | el informe de fallos de Windows dice el hilo y quién llamó, y escribe `user\crash.dmp` (candidato upstream) |
| 0007 | mesh raycaster copies activo por defecto también en U1 (candidato upstream) |
| 0008 | `run.py` arranca `UNDS\UNDS.exe` con el juego cuando está instalado |

El protocolo está documentado al principio de `src/runtime_unds.c` con 0001–0004 aplicados.

## Limitaciones conocidas

- Estado del juego solo en Uncharted 1 (1.00 jugada, patrones de 1.02 verificados); U2/U3 usan el modo
  genérico y se han probado menos.
- Los nombres de armas de los `.ini` son conjeturas por su comportamiento; lo que cuenta son los ids.
- Capítulo de la moto acuática (U1): el jugador puede morir por la explosión de su propia granada lejana.
  Pasa también con UNDS apagado; es un problema del port o del juego en estudio, no de UNDS.

## Créditos y licencia

- [uncharted_pc](https://github.com/Supermedo/uncharted_pc) de SuperMedo (GPL-2.0), el port sobre el que se
  construye.
- Inspirado en el mod RE4R DualSense Enhanced Edition; sintaxis de efectos de gatillo como los modos v3 de DSX.
- UNDS de [marks-jhojan](https://github.com/marks-jhojan), GPL-2.0 (ver `LICENSE`), como el port.

UNDS no está afiliado a Sony Interactive Entertainment ni a Naughty Dog y no incluye archivos del juego:
necesitas tu propia copia.
