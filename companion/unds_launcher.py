#!/usr/bin/env python3
"""UNDS — lanzador y companion (gatillos, lightbar y hápticos del DualSense) para el port uncharted_pc.

Lo normal: NADA. Con el zip de UNDS extraído sobre el port, run.py (parche 0008) arranca UNDS.exe con el
juego, lo abras como lo abras (Play del launcher, accesos directos, Steam):
    run.py -> UNDS.exe --env                       variables que quiere UNDS (o código 1 si auto = no)
    run.py -> UNDS.exe --attach <pid de run.py>    companion en una consola minimizada (window = hidden en
                                                   unds_launcher.ini: sin ventana); siempre con log en
                                                   logs\\unds.log (el anterior en unds.prev.log); termina con run.py

Doble clic en UNDS.exe (sin argumentos) sigue sirviendo, con comprobaciones interactivas:
  1. Busca la release del port (Uncharted.exe + bin\\bb-probe.exe): --game, [game] dir de unds_launcher.ini,
     la carpeta de UNDS.exe y la de encima; si no la encuentra, la pregunta con un diálogo y la guarda.
  2. Comprueba: que bb-probe.exe trae UNDS con el canal fx, que no queda un juego anterior abierto (retiene
     ~6 GB), la memoria libre (RAM + paginación) y que DSX está cerrado (abierto apaga los hápticos).
  3. Pone las variables UNDS ([environment] del .ini; no pisa las que ya existan): las hereda
     Uncharted.exe -> run.py -> bb-probe.exe (game_environment() parte de os.environ).
  4. Abre Uncharted.exe (el launcher del port: eliges juego y Play) y corre el companion en esta consola.
  5. Vigila user\\last_run.log: arranque defectuoso (red zone), pérdida de datos, aserción o fallo del port
     -> aviso aquí, pitido y lightbar roja parpadeando (visible con el juego a pantalla completa).
  6. Se cierra solo cuando ya no quedan ni el launcher ni el juego (o con Ctrl+C / la X de la consola,
     soltando antes los gatillos).

    UNDS.exe                       # lo normal
    UNDS.exe --game D:\\Juegos\\uncharted-windows
    UNDS.exe --no-game             # solo el companion (el juego lo abres tú, con UNDS_EVENTS puesto)
    UNDS.exe --record              # además graba el JSONL en recordings\\ (para reportar fallos)
    UNDS.exe --check               # solo las comprobaciones
    UNDS.exe --selftest            # bibliotecas empaquetadas y dispositivos de audio
    UNDS.exe --env                 # lo que run.py le pregunta
    UNDS.exe --studio              # UNDS Studio: calibración visual de gatillos, hápticos y lightbar
Las demás opciones pasan al companion (unds.py --help): --no-haptics, --no-triggers, -v, --show ...
"""
from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

VERSION = "1.0.0"
PORT_TAG = "v0.3.9"
GAME_EXE = "Uncharted.exe"
PROBE = Path("bin") / "bb-probe.exe"
DEFAULT_ENV = {"UNDS_EVENTS": "127.0.0.1:47111", "UNDS_TRIGGER": "30,95", "UNDS_RUMBLE": "0"}
SETTINGS_DEFAULTS = {
    "game": {"dir": ""},
    "launcher": {"auto": "yes", "window": "console", "record": "no", "min_free_gb": "11.5", "alert_seconds": "20", "close_dsx": "ask", "close_old_game": "ask",
                 "red_zone_threshold": "300"},
}
# Líneas de <user>\last_run.log (src/probe.c, src/red_zone.c de v0.3.9 + parches UNDS).
ATTACH_BIND_WAIT_S = 15.0       # "Apply and restart": el companion anterior tarda en soltar el puerto
RE_RED_ZONE = re.compile(r"Red zone: \d+ of \d+ guest functions[^\r\n]*?, (\d+) left as they were")
RE_ASSERT = re.compile(r"\*\*\* ASSERTION: ([^\r\n]+)")
RE_FAULT = re.compile(r"^Fault: ([^\r\n]+)", re.M)
DATA_LOSS = "its data may be lost"
HEADER_BYTES = 256


def base_dir() -> Path:
    """Carpeta de UNDS.exe (congelado) o de este script: ahí viven los .ini y recordings\\."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


# ---- ajustes -------------------------------------------------------------------------------------
class Settings:
    def __init__(self, path: Path):
        self.path = path
        self.parser = configparser.ConfigParser(interpolation=None)
        self.parser.optionxform = str                 # nombres de variables tal cual (UNDS_TRIGGER)
        self.parser.read_dict(SETTINGS_DEFAULTS)
        if path.is_file():
            try:
                self.parser.read(path, encoding="utf-8")
            except (configparser.Error, UnicodeDecodeError) as error:
                print(f"[UNDS] {path} no se puede leer ({error}): uso los valores por defecto.")
        if not self.parser.has_section("environment"):
            self.parser.add_section("environment")
            for key, value in DEFAULT_ENV.items():
                self.parser.set("environment", key, value)

    def get(self, section: str, key: str) -> str:
        return self.parser.get(section, key, fallback=SETTINGS_DEFAULTS.get(section, {}).get(key, "")).strip()

    def number(self, section: str, key: str) -> float:
        try:
            return float(self.get(section, key).replace(",", "."))
        except ValueError:
            return float(SETTINGS_DEFAULTS[section][key])

    def environment(self) -> dict[str, str]:
        return {k: v.strip() for k, v in self.parser.items("environment") if v.strip()}

    def save_game_dir(self, folder: Path) -> None:
        """Solo cambia la línea dir = de [game] (los comentarios del .ini se conservan)."""
        self.parser.set("game", "dir", str(folder))
        line = f"dir = {folder}"
        try:
            text = self.path.read_text(encoding="utf-8") if self.path.is_file() else ""
            section = re.search(r"(?mi)^\[game\][ \t]*$", text)
            if section is None:
                text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + f"[game]\n{line}\n"
            else:
                tail = text[section.end():]
                nxt = re.search(r"(?m)^\[", tail)
                body = tail[:nxt.start()] if nxt else tail
                found = re.search(r"(?m)^dir[ \t]*[=:].*$", body)
                if found:
                    body = body[:found.start()] + line + body[found.end():]
                else:
                    body = "\n" + line + body
                text = text[:section.end()] + body + (tail[nxt.start():] if nxt else "")
            self.path.write_text(text, encoding="utf-8")
        except OSError as error:
            print(f"[UNDS] No pude guardar {self.path}: {error}")


# ---- la release del port -------------------------------------------------------------------------
def is_game_dir(folder: Path | None) -> bool:
    return bool(folder) and (folder / GAME_EXE).is_file() and (folder / PROBE).is_file()


def find_game_dir(explicit: str, saved: str, here: Path) -> Path | None:
    if explicit:                                      # una carpeta pedida: esa o ninguna
        return Path(explicit).resolve() if is_game_dir(Path(explicit)) else None
    candidates = []
    if saved:
        candidates.append(Path(saved))
    candidates += [here, here.parent, here.parent.parent]
    for folder in candidates:
        if is_game_dir(folder):
            return folder.resolve()
    return ask_game_dir()


def ask_game_dir() -> Path | None:
    print("[UNDS] No encuentro la release del port (la carpeta con Uncharted.exe y bin\\bb-probe.exe).")
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        return None
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    chosen = filedialog.askdirectory(title="UNDS: carpeta del port (la que tiene Uncharted.exe)", mustexist=True)
    root.destroy()
    folder = Path(chosen) if chosen else None
    return folder.resolve() if is_game_dir(folder) else None


def probe_features(probe: Path) -> set[str]:
    """Cadenas que delatan los parches UNDS dentro de bb-probe.exe (getenv de runtime_unds.c)."""
    try:
        data = probe.read_bytes()
    except OSError:
        return set()
    return {name for name in ("UNDS_EVENTS", "UNDS_FX", "UNDS_STATE") if name.encode() in data}


def launcher_settings() -> dict:
    """%APPDATA%\\uncharted-launcher\\settings.json del launcher del port (carpeta de partidas, Fixes)."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return {}
    try:
        return json.loads((Path(appdata) / "uncharted-launcher" / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def log_path(game_dir: Path, settings: dict) -> Path:
    user = os.environ.get("BB_USER_DIR") or str(settings.get("user_dir") or "").strip()
    return (Path(user) if user else game_dir / "user") / "last_run.log"


# ---- Windows ------------------------------------------------------------------------------------
def running(image: str) -> int:
    """Procesos con ese nombre de imagen (tasklist); 0 fuera de Windows o si falla."""
    if os.name != "nt":
        return 0
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH", "/FO", "CSV"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    return sum(1 for line in out.splitlines() if line.lower().startswith(f'"{image.lower()}"'))


def kill(image: str) -> None:
    subprocess.run(["taskkill", "/IM", image, "/F"], capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def free_commit_gb() -> float | None:
    """Memoria que aún se puede comprometer (RAM + archivo de paginación), en GB."""
    if os.name != "nt":
        return None
    import ctypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return status.ullAvailPageFile / 2**30


def beep() -> None:
    if os.name != "nt":
        print("\a", end="", flush=True)
        return
    try:
        import winsound
        for _ in range(3):
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            time.sleep(0.35)
    except (ImportError, RuntimeError):
        pass


def ask(question: str, policy: str, default: bool = True) -> bool:
    """policy: yes | no | ask (consola; sin consola interactiva, el valor por defecto)."""
    if policy in ("yes", "si", "sí"):
        return True
    if policy == "no":
        return False
    if not sys.stdin or not sys.stdin.isatty():
        return default
    hint = "[S/n]" if default else "[s/N]"
    try:
        answer = input(f"{question} {hint} ").strip().lower()
    except EOFError:
        return default
    return default if not answer else answer[0] in "sy"


def port_in_use(host: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        sock.close()


def process_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x00100000, False, pid)        # SYNCHRONIZE
        if not handle:
            return False
        state = kernel32.WaitForSingleObject(handle, 0)
        kernel32.CloseHandle(handle)
        return state == 0x102                                        # WAIT_TIMEOUT = sigue vivo
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def wait_for_port(bus: tuple[str, int], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while port_in_use(*bus):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)
    return True


def window_mode(settings: Settings) -> str:
    """Cómo corre el companion que arranca run.py: console (consola minimizada, por defecto) | hidden."""
    return "hidden" if settings.get("launcher", "window").lower() == "hidden" else "console"


class Tee:
    """Escribe en la consola y en el log a la vez (modo --attach con consola)."""

    def __init__(self, console, handle):
        self.console, self.handle = console, handle

    def write(self, text: str) -> int:
        for stream in (self.console, self.handle):
            if stream is not None:
                try:
                    stream.write(text)
                except (OSError, ValueError):
                    pass
        return len(text)

    def flush(self) -> None:
        for stream in (self.console, self.handle):
            if stream is not None:
                try:
                    stream.flush()
                except (OSError, ValueError):
                    pass

    def reconfigure(self, **kwargs) -> None:
        reconfigure = getattr(self.console, "reconfigure", None)
        if reconfigure:
            reconfigure(**kwargs)

    def isatty(self) -> bool:
        return bool(self.console is not None and self.console.isatty())


def redirect_output(path: Path, keep_console: bool = False) -> None:
    """Salida a logs\\unds.log (el anterior queda en unds.prev.log); con keep_console también a la consola."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            os.replace(path, path.with_suffix(".prev.log"))
        handle = open(path, "w", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        return                                        # sin log: sigue igual
    if keep_console:
        sys.stdout, sys.stderr = Tee(sys.stdout, handle), Tee(sys.stderr, handle)
    else:
        sys.stdout = sys.stderr = handle


def enabled(value: str) -> bool:
    return value.strip().lower() not in ("no", "0", "off", "false")


# ---- vigilancia del log --------------------------------------------------------------------------
class LogWatcher:
    """Lee last_run.log según crece y avisa una vez por proceso del port de cada problema."""

    def __init__(self, path: Path, threshold: int, notify, started: float | None = None):
        self.path, self.threshold, self.notify = path, threshold, notify
        self.started = time.time() if started is None else started
        self.offset = 0
        self.pending = ""
        self.header = b""                             # primeros bytes: cambian si el port reescribe el log
        self.reset()

    def reset(self) -> None:
        self.red_zone_seen = self.loss_warned = self.assert_warned = self.fault_warned = False

    def poll(self) -> None:
        try:
            stat = self.path.stat()
        except OSError:
            return
        if stat.st_mtime < self.started - 1:
            return                                    # el log de una partida anterior
        if stat.st_size == self.offset:
            return
        try:
            with self.path.open("rb") as handle:
                header = handle.read(HEADER_BYTES)
                # El port reescribe el log en cada arranque (cambio de juego = proceso nuevo): más corto que lo
                # ya leído, o con otro principio si creció deprisa.
                if stat.st_size < self.offset or (self.header and header[:len(self.header)] != self.header):
                    self.offset, self.pending, self.header = 0, "", b""
                    self.reset()
                if len(self.header) < len(header):
                    self.header = header
                handle.seek(self.offset)
                chunk = handle.read(stat.st_size - self.offset)
        except OSError:
            return
        self.offset += len(chunk)
        text = self.pending + chunk.decode("utf-8", errors="replace")
        lines = text.split("\n")
        self.pending = lines.pop()                    # línea a medio escribir: para la próxima vez
        self.scan("\n".join(lines))

    def scan(self, text: str) -> None:
        if not self.red_zone_seen:
            match = RE_RED_ZONE.search(text)
            if match:
                self.red_zone_seen = True
                left = int(match.group(1))
                if left > self.threshold:
                    self.notify("arranque", f"ARRANQUE DEFECTUOSO: el port dejó {left} instrucciones del juego sin "
                                f"preparar (lo normal es menos de 100). Esta partida puede perder datos o colgarse "
                                f"al guardar: cierra el juego y vuelve a abrirlo antes de avanzar.")
                else:
                    self.notify("ok", f"arranque correcto ({left} instrucciones sin reescribir)")
        if not self.loss_warned and DATA_LOSS in text:
            self.loss_warned = True
            self.notify("datos", "el port avisa de que el juego pudo PERDER DATOS. Si algo va raro o se congela, "
                        "cierra y vuelve a abrir; el último punto de control debería estar guardado.")
        if not self.assert_warned:
            match = RE_ASSERT.search(text)
            if match:
                self.assert_warned = True
                what = match.group(1).strip()
                tip = (" Arreglo: launcher > Fixes > Mesh raycaster copies en Automatic u On."
                       if "iSubMesh" in what else "")
                self.notify("asercion", f"el juego se detuvo con una aserción: {what}.{tip}")
        if not self.fault_warned:
            match = RE_FAULT.search(text)
            if match:
                self.fault_warned = True
                self.notify("fallo", f"el port se cayó ({match.group(1).strip()[:120]}). Para reportarlo: "
                            f"{self.path} y {self.path.with_name('crash.dmp')}.")


# ---- consola ------------------------------------------------------------------------------------
_handler_ref = None


def install_close_handler(stop) -> None:
    """La X de la consola (o cerrar sesión) suelta los gatillos antes de que Windows mate el proceso."""
    global _handler_ref
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    def handler(kind):
        if kind in (2, 5, 6):                         # CTRL_CLOSE / LOGOFF / SHUTDOWN
            stop(wait=3.0)
            return True
        return False                                  # Ctrl+C: KeyboardInterrupt normal

    _handler_ref = handler
    ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)


def setup_windows_console() -> None:
    if os.name != "nt":
        return
    import ctypes
    ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    ctypes.windll.kernel32.SetConsoleTitleW(f"UNDS {VERSION}")


# ---- principal ----------------------------------------------------------------------------------
def parse(argv: list[str] | None) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(description=f"UNDS {VERSION}: abre el port con UNDS y el companion.",
                                     epilog="Las demás opciones pasan al companion (unds.py --help).")
    parser.add_argument("--game", default="", help="carpeta de la release del port (con Uncharted.exe)")
    parser.add_argument("--no-game", action="store_true", help="no abrir el juego: solo el companion")
    parser.add_argument("--record", action="store_true", help="grabar el JSONL en recordings\\")
    parser.add_argument("--check", action="store_true", help="solo las comprobaciones, sin abrir nada")
    parser.add_argument("--settings", default="", help="ajustes del lanzador (unds_launcher.ini junto al exe)")
    parser.add_argument("--selftest", action="store_true", help="comprueba las bibliotecas (numpy, audio, HID) y sale")
    parser.add_argument("--studio", action="store_true",
                        help="abre UNDS Studio (calibración visual en el navegador; las demás opciones van al Studio)")
    parser.add_argument("--env", action="store_true", help="imprime las variables UNDS para run.py (código 1 = auto = no)")
    parser.add_argument("--attach", type=int, default=0, metavar="PID",
                        help="modo run.py: sin preguntas ni abrir el juego; termina cuando muere PID")
    parser.add_argument("--log", default="", help="last_run.log a vigilar (por defecto <port>\\user\\last_run.log)")
    return parser.parse_known_args(argv)


def preflight(opts: argparse.Namespace, settings: Settings, here: Path) -> tuple[Path | None, list[str]]:
    """Comprobaciones antes de abrir nada. (carpeta del port o None, problemas que impiden seguir)."""
    problems: list[str] = []
    game_dir = None
    if not opts.no_game:
        game_dir = find_game_dir(opts.game, settings.get("game", "dir"), here)
        if game_dir is None:
            problems.append(f"no encuentro la release del port {PORT_TAG} (Uncharted.exe + bin\\bb-probe.exe). "
                            f"Pon UNDS junto a Uncharted.exe o usa --game <carpeta>.")
            return None, problems
        print(f"[UNDS] Port: {game_dir}")
        if str(game_dir) != settings.get("game", "dir"):
            settings.save_game_dir(game_dir)
        features = probe_features(game_dir / PROBE)
        if "UNDS_EVENTS" not in features:
            problems.append(f"{game_dir / PROBE} es el del port sin UNDS: extrae el zip de UNDS ENCIMA de la "
                            f"release {PORT_TAG} (sobrescribiendo).")
        elif "UNDS_FX" not in features:
            problems.append(f"{game_dir / PROBE} trae un UNDS antiguo sin canal fx: extrae de nuevo el zip de "
                            f"UNDS {VERSION} encima de la release.")
        fixes = launcher_settings().get("fix_u2_mesh")
        if fixes == "on":
            print("[UNDS] Aviso: Fixes > Mesh raycaster copies está en On (también en U3, cuesta FPS). Con UNDS "
                  "'Automatic' ya lo activa en U1 y U2.")
        elif fixes == "off":
            print("[UNDS] Aviso: Fixes > Mesh raycaster copies está en Off: U1 y U2 pueden detenerse con "
                  "'iSubMesh' tras un punto de control. Recomendado: Automatic.")
    if running("bb-probe.exe"):
        print("[UNDS] Hay un juego (bb-probe.exe) abierto de antes: retiene ~6 GB de memoria.")
        if not opts.no_game and ask("¿Cerrarlo ahora?", settings.get("launcher", "close_old_game")):
            kill("bb-probe.exe")
            time.sleep(3)
    free = free_commit_gb()
    need = settings.number("launcher", "min_free_gb")
    if free is not None and free < need and not opts.no_game:
        print(f"[UNDS] Solo hay {free:.1f} GB libres de memoria (RAM + paginación); el juego necesita ~{need:g} GB "
              f"(6 GB al arrancar + ~5 GB al jugar) y puede cerrarse con 'error 1450'. Cierra otros programas "
              f"(máquinas virtuales, WSL, navegador) o sube el archivo de paginación.")
        if not ask("¿Abrir de todas formas?", "ask", default=False):
            problems.append("memoria insuficiente")
    if running("DSX.exe"):
        print("[UNDS] DSX está abierto: apaga los hápticos del mando y pelea por los gatillos.")
        if ask("¿Cerrar DSX ahora?", settings.get("launcher", "close_dsx")):
            kill("DSX.exe")
            time.sleep(2)
    return game_dir, problems


def selftest() -> int:
    """Que el exe trae lo que necesita: numpy, PortAudio con WASAPI y hidapi (opcional)."""
    failures = 0
    try:
        import numpy
        print(f"[UNDS] numpy {numpy.__version__}")
    except ImportError as error:
        failures += 1
        print(f"[UNDS] ERROR numpy: {error}")
    try:
        import sounddevice
        apis = [api["name"] for api in sounddevice.query_hostapis()]
        outputs = sum(1 for d in sounddevice.query_devices() if d["max_output_channels"] > 0)
        print(f"[UNDS] sounddevice {sounddevice.__version__}, PortAudio {sounddevice.get_portaudio_version()[1]}; "
              f"APIs: {', '.join(apis)}; {outputs} salidas")
        if os.name == "nt" and not any("WASAPI" in api for api in apis):
            failures += 1
            print("[UNDS] ERROR: PortAudio sin WASAPI: los hápticos no pueden abrir el DualSense.")
    except Exception as error:                        # OSError de PortAudio, ImportError...
        failures += 1
        print(f"[UNDS] ERROR sounddevice: {error}")
    try:
        import hid                                    # noqa: F401
        print("[UNDS] hidapi presente (rescate de gatillos si el port se cae)")
    except ImportError:
        print("[UNDS] hidapi ausente (opcional)")
    try:
        import unds                                   # noqa: F401
        print("[UNDS] companion importado")
    except Exception as error:
        failures += 1
        print(f"[UNDS] ERROR companion: {error}")
    try:
        import unds_studio
        print(f"[UNDS] Studio listo ({unds_studio.html_path().name})")
    except Exception as error:
        failures += 1
        print(f"[UNDS] ERROR Studio: {error}")
    print("[UNDS] selftest: " + ("OK" if not failures else f"{failures} fallo(s)"))
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    opts, rest = parse(argv)
    here = base_dir()
    settings = Settings(Path(opts.settings) if opts.settings else here / "unds_launcher.ini")
    if opts.env:                                      # pregunta de run.py: sin consola, solo NOMBRE=valor
        if not enabled(settings.get("launcher", "auto")):
            return 1
        env = {"UNDS_EVENTS": DEFAULT_ENV["UNDS_EVENTS"], **settings.environment(), "UNDS_WINDOW": window_mode(settings)}
        for key, value in env.items():
            if key.startswith("UNDS_"):
                print(f"{key}={value}")
        return 0
    if opts.selftest:
        setup_windows_console()
        return selftest()
    if opts.studio:
        setup_windows_console()
        import unds_studio
        return unds_studio.main(list(rest), here)
    attached = opts.attach > 0
    if attached:                                      # arrancado por el juego: siempre queda logs\\unds.log
        redirect_output(here / "logs" / "unds.log", keep_console=window_mode(settings) == "console")
    setup_windows_console()
    print(f"UNDS {VERSION} — DualSense para Uncharted: The Nathan Drake Collection (port {PORT_TAG})"
          + (" — arrancado con el juego" if attached else ""))

    env = settings.environment()
    events = os.environ.get("UNDS_EVENTS") or env.get("UNDS_EVENTS", DEFAULT_ENV["UNDS_EVENTS"])
    host, _, port_text = events.rpartition(":")
    try:
        bus = (host or "127.0.0.1", int(port_text))
    except ValueError:
        print(f"[UNDS] UNDS_EVENTS mal formado: {events!r} (esperaba 127.0.0.1:47111)")
        return 2

    if attached:
        game_dir = Path(opts.game).resolve() if opts.game else None
        if running("DSX.exe"):
            print("[UNDS] DSX está abierto: apaga los hápticos del mando y pelea por los gatillos.")
            if settings.get("launcher", "close_dsx").lower() in ("yes", "si", "sí"):
                kill("DSX.exe")
            else:
                print("[UNDS] Ciérralo (también desde la bandeja del sistema), o pon close_dsx = yes en "
                      "unds_launcher.ini para que UNDS lo cierre.")
        if not wait_for_port(bus, ATTACH_BIND_WAIT_S):
            print(f"[UNDS] {bus[0]}:{bus[1]} sigue ocupado: ya hay otro UNDS con este juego.")
            return 2
    else:
        if port_in_use(*bus):
            print(f"[UNDS] Ya hay un UNDS (u otro programa) escuchando en {bus[0]}:{bus[1]}: no abro otro.")
            return 2
        game_dir, problems = preflight(opts, settings, here)
        if problems:
            for problem in problems:
                print(f"[UNDS] ERROR: {problem}")
            return 1
        if opts.check:
            print("[UNDS] Comprobaciones superadas.")
            return 0

    import unds                                       # numpy / sounddevice: solo a partir de aquí
    companion_argv = ["--config", str(here / "unds_triggers.ini"), "--haptics-config", str(here / "unds_haptics.ini"),
                      "--out", str(here / "recordings"), "--host", bus[0], "--port", str(bus[1])]
    if not (opts.record or enabled(settings.get("launcher", "record")) and settings.get("launcher", "record")):
        companion_argv.append("--no-record")
    args = unds.build_parser(here).parse_args(companion_argv + rest)
    unds.setup_console()
    companion = unds.create_companion(args)
    if companion is None:
        return 2

    if not attached:
        for key, value in env.items():
            os.environ.setdefault(key, value)
        os.environ["UNDS_EVENTS"] = f"{bus[0]}:{bus[1]}"
    done = threading.Event()

    def stop(wait: float = 0.0) -> None:
        companion.stop = True
        if wait:
            done.wait(wait)

    install_close_handler(stop)
    alert_s = settings.number("launcher", "alert_seconds")

    def notify(kind: str, text: str) -> None:
        companion.say(f"UNDS  {text}")
        if kind != "ok":
            engine = getattr(companion, "engine", None)
            if engine is not None and not args.no_dsx:
                engine.alert(alert_s)
            threading.Thread(target=beep, daemon=True).start()

    log = Path(opts.log) if opts.log else (log_path(game_dir, launcher_settings()) if game_dir else None)
    lw = LogWatcher(log, int(settings.number("launcher", "red_zone_threshold")), notify) if log else None
    if attached:
        def alive() -> bool:
            return process_alive(opts.attach)
        why = "el juego terminó: cierro el companion."
    elif game_dir is not None:
        try:
            subprocess.Popen([str(game_dir / GAME_EXE)], cwd=str(game_dir))
        except OSError as error:
            print(f"[UNDS] No pude abrir {game_dir / GAME_EXE}: {error}")
            return 1
        print("[UNDS] Launcher del port abierto: elige el juego y pulsa Play. Esta ventana es el companion; se "
              "cierra sola al cerrar el launcher y el juego (o Ctrl+C).")
        seen = [False]

        def alive() -> bool:
            count = running(GAME_EXE) + running("bb-probe.exe")
            seen[0] = seen[0] or count > 0
            return count > 0 or not seen[0]
        why = "launcher y juego cerrados: cierro el companion."
    else:
        alive = None
        why = ""

    if lw is not None or alive is not None:
        def watch() -> None:
            gone, last_check = 0, 0.0
            while not done.is_set():
                if lw is not None:
                    lw.poll()
                now = time.monotonic()
                if alive is not None and now - last_check >= (1.0 if attached else 5.0):
                    last_check = now
                    gone = 0 if alive() else gone + 1
                    if gone >= (1 if attached else 2):
                        if lw is not None:
                            lw.poll()                 # lo último del log antes de irse
                        companion.say(f"UNDS  {why}")
                        stop()
                        return
                done.wait(1.0 if attached else 2.0)

        threading.Thread(target=watch, name="unds-watch", daemon=True).start()
    try:
        return companion.run()
    finally:
        done.set()


def entry() -> int:
    """Doble clic en UNDS.exe: si algo falla, la consola espera a Enter en vez de cerrarse sin dejar leer."""
    try:
        code = main()
    except SystemExit as stop:                        # argparse (--help, opción mala)
        code = stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 2)
    except Exception:                                 # noqa: BLE001 — se muestra entero para reportarlo
        import traceback
        traceback.print_exc()
        code = 3
    attached = "--attach" in sys.argv or "--env" in sys.argv   # sin nadie delante: no esperar a Enter
    if code and not attached and getattr(sys, "frozen", False) and sys.stdin and sys.stdin.isatty():
        try:
            input("\n[UNDS] Pulsa Enter para cerrar esta ventana...")
        except (EOFError, KeyboardInterrupt):
            pass
    return code


if __name__ == "__main__":
    sys.exit(entry())
