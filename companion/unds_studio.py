#!/usr/bin/env python3
"""UNDS Studio — calibración visual de gatillos, hápticos y lightbar, probándolo en el mando.

Un servidor local (solo 127.0.0.1) sirve studio.html al navegador. Todo lo que se cambia se guarda al
momento en unds_triggers.ini / unds_haptics.ini (validado con el mismo código que usa el companion, con
copia de seguridad al abrir): con el juego abierto, el companion lo recarga solo.

Con el juego CERRADO el Studio maneja el mando él mismo (USB):
  - gatillos y lightbar por HID (el mismo reporte que unds_hid.py), leyendo a la vez L2/R2 y botones;
  - hápticos por WASAPI (canales 3-4, como unds_haptics.py) con las voces del .ini;
  - galería de tiro: eliges un arma, aprietas R2 y se siente como en el juego (retroceso por fases, voz,
    ráfagas de las automáticas, cargador; □ recarga).
Con el juego ABIERTO solo edita (el juego es dueño del mando); los hápticos de prueba siguen sonando.

    UNDS.exe --studio            (o: python unds_studio.py)
    python unds_studio.py --demo # sin mando: un gatillo simulado que se mueve solo (para ver la interfaz)
"""
from __future__ import annotations

import argparse
import configparser
import json
import math
import os
import re
import shutil
import socket
import sys
import threading
import time
import webbrowser
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import unds_haptics as H  # noqa: E402
import unds_triggers as T  # noqa: E402
from unds_dsx import V3_MODES, parse_effect  # noqa: E402
from unds_hid import (BUS_USB, RELEASE_RGB, build_report, encode_effect, find_controllers,  # noqa: E402
                      hid)

VERSION = "1.0.0"
DEFAULT_PORT = 47112
GAME = "u1"                       # perfil del juego con el que se calibra (estado real: U1)
MODE_NAMES = {mode: name for name, (mode, _) in V3_MODES.items()}
PARAM_LABELS = {
    "OFF": [],
    "FEEDBACK": ["inicio", "fuerza"],
    "WEAPON": ["inicio", "fin", "fuerza"],
    "VIBRATION": ["inicio", "amplitud", "frecuencia"],
    "SLOPE_FEEDBACK": ["inicio", "fin", "fuerza inicial", "fuerza final"],
    "MULTIPLE_POSITION_FEEDBACK": [f"zona {i}" for i in range(10)],
    "MULTIPLE_POSITION_VIBRATION": ["frecuencia"] + [f"zona {i}" for i in range(10)],
    "MACHINE": ["inicio", "fin", "fuerza A", "fuerza B", "Hz", "periodo (0,1 s)"],
}
MODE_HELP = {
    "OFF": "Gatillo libre.",
    "FEEDBACK": "Resistencia constante desde una zona hasta el final.",
    "WEAPON": "Tope duro entre dos zonas que se rompe al pasarlo: el clic de un arma.",
    "VIBRATION": "Vibra a partir de una zona.",
    "SLOPE_FEEDBACK": "Resistencia que crece (o baja) entre dos zonas.",
    "MULTIPLE_POSITION_FEEDBACK": "Una fuerza por zona.",
    "MULTIPLE_POSITION_VIBRATION": "Una amplitud de vibración por zona.",
    "MACHINE": "Oscila entre dos fuerzas (efecto no oficial del mando; solo port/HID).",
}
CLIP = {"pistola": 8, "revolver": 6, "subfusil": 20, "automatica": 30, "escopeta": 6,
        "lanzagranadas": 1, "torreta": 0, "granada": 3}          # 0 = infinita
RELOAD_S = 1.3
REASSERT_S = 2.0
TICK_S = 0.004
VOICE_POINTS = 600


# ---- .ini con comentarios ------------------------------------------------------------------------
SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
KEY_RE = re.compile(r"^(\s*)([^\s;#=\[][^=]*?)(\s*=\s*)(.*)$")


def split_comment(rest: str) -> tuple[str, str]:
    """'WEAPON 3 4 5     ; comentario' -> ('WEAPON 3 4 5', '     ; comentario') (como configparser:
    un ; o # con espacio delante)."""
    match = re.search(r"\s[;#]", rest)
    if not match:
        return rest.rstrip(), rest[len(rest.rstrip()):]
    return rest[:match.start()].rstrip(), rest[len(rest[:match.start()].rstrip()):]


class IniDoc:
    """Edición línea a línea que conserva comentarios, orden y alineación."""

    def __init__(self, text: str):
        self.newline = "\r\n" if "\r\n" in text else "\n"
        self.lines = text.splitlines()

    @classmethod
    def read(cls, path: Path) -> "IniDoc":
        return cls(path.read_text(encoding="utf-8") if path.exists() else "")

    def text(self) -> str:
        return self.newline.join(self.lines) + self.newline

    def _section_span(self, section: str) -> tuple[int, int] | None:
        start = None
        for i, line in enumerate(self.lines):
            match = SECTION_RE.match(line)
            if match:
                if start is not None:
                    return start, i
                if match.group(1).strip().lower() == section.strip().lower():
                    start = i
        return (start, len(self.lines)) if start is not None else None

    def _find(self, section: str, key: str) -> int | None:
        span = self._section_span(section)
        if span is None:
            return None
        for i in range(span[0] + 1, span[1]):
            match = KEY_RE.match(self.lines[i])
            if match and not self.lines[i].lstrip().startswith((";", "#")) \
                    and match.group(2).strip().lower() == key.lower():
                return i
        return None

    def get(self, section: str, key: str) -> str | None:
        i = self._find(section, key)
        if i is None:
            return None
        return split_comment(KEY_RE.match(self.lines[i]).group(4))[0]

    def set(self, section: str, key: str, value: str) -> None:
        value = value.strip()
        i = self._find(section, key)
        if i is not None:
            indent, name, eq, rest = KEY_RE.match(self.lines[i]).groups()
            old, comment = split_comment(rest)
            if comment.strip():
                # mantiene la columna del comentario si cabe
                pad = max(1, len(old) + len(comment) - len(comment.lstrip()) - len(value))
                comment = " " * pad + comment.lstrip()
            self.lines[i] = f"{indent}{name}{eq}{value}{comment}"
            return
        span = self._section_span(section)
        if span is None:
            while self.lines and not self.lines[-1].strip():
                self.lines.pop()
            self.lines += ["", f"[{section}]", f"{key} = {value}"]
            return
        last = span[0]
        for j in range(span[0] + 1, span[1]):
            if self.lines[j].strip() and not self.lines[j].lstrip().startswith((";", "#")):
                last = j
        self.lines.insert(last + 1, f"{key} = {value}")

    def unset(self, section: str, key: str) -> bool:
        i = self._find(section, key)
        if i is None:
            return False
        del self.lines[i]
        span = self._section_span(section)
        if span is not None and section.lower().startswith("clase "):
            start, end = span
            if not any(ln.strip() for ln in self.lines[start + 1:end]):   # [clase X] vacía: fuera
                del self.lines[start:end]
                if start > 0 and not self.lines[start - 1].strip():
                    del self.lines[start - 1]
        return True

    def sections(self) -> list[dict]:
        """[{name, keys: [{key, value, doc}]}] para el editor avanzado; doc = comentario de la línea y
        las líneas de comentario justo encima."""
        out: list[dict] = []
        current: dict | None = None
        notes: list[str] = []
        for line in self.lines:
            stripped = line.strip()
            match = SECTION_RE.match(line)
            if match:
                current = {"name": match.group(1).strip(), "keys": []}
                out.append(current)
                notes = []
                continue
            if stripped.startswith((";", "#")):
                notes.append(stripped.lstrip(";#").strip())
                continue
            if not stripped:
                notes = []
                continue
            match = KEY_RE.match(line)
            if match and current is not None:
                value, comment = split_comment(match.group(4))
                doc = " ".join(n for n in notes[-3:] if n)
                inline = comment.strip().lstrip(";#").strip()
                current["keys"].append({"key": match.group(2).strip(), "value": value, "doc": inline or doc})
                notes = []
            elif current is not None and line.startswith((" ", "\t")):
                continue                                  # continuación de un comentario largo
        return out


# ---- de efectos y voces a datos para dibujar -----------------------------------------------------
def effect_zones(text: str) -> dict:
    """Efecto de gatillo -> zonas 0-9 con fuerza y vibración (0-8), para el dibujo del gatillo."""
    effect = parse_effect(text)
    name = MODE_NAMES.get(effect.mode)
    zones = [{"f": 0, "v": 0} for _ in range(10)]
    info = {"ok": True, "text": effect.text, "name": name or effect.text.split()[0], "params": list(effect.params),
            "legacy": name is None, "freq": 0, "break": None, "zones": zones}
    p = effect.params
    if name == "FEEDBACK":
        for i in range(p[0], 10):
            zones[i]["f"] = p[1]
    elif name == "WEAPON":
        for i in range(p[0], p[1]):
            zones[i]["f"] = p[2]
        info["break"] = p[1]
    elif name == "VIBRATION":
        for i in range(p[0], 10):
            zones[i]["v"] = p[1]
        info["freq"] = p[2]
    elif name == "SLOPE_FEEDBACK":
        start, end, s0, s1 = p
        slope = (s1 - s0) / (end - start)
        for i in range(start, 10):
            zones[i]["f"] = round(s0 + slope * (i - start)) if i <= end else s1
    elif name == "MULTIPLE_POSITION_FEEDBACK":
        for i, v in enumerate(p):
            zones[i]["f"] = v
    elif name == "MULTIPLE_POSITION_VIBRATION":
        info["freq"] = p[0]
        for i, v in enumerate(p[1:]):
            zones[i]["v"] = v
    elif name == "MACHINE":
        start, end, a, b, hz, _period = p
        for i in range(start, end + 1):
            zones[i]["f"] = max(a, b)
            zones[i]["v"] = abs(a - b) or 1
        info["freq"] = hz
    return info


def kick_phases(text: str, default_ms: int) -> list[dict]:
    return [{"effect": str(e), "ms": round(s * 1000)} for e, s in T.parse_kick(text, default_ms, "kick")]


def voice_wave(text: str, points: int = VOICE_POINTS) -> dict:
    """Voz háptica -> envolvente izquierda/derecha (máximo absoluto por tramo) para dibujarla."""
    layers = H.parse_voice(text)
    if not layers:
        return {"ok": True, "ms": 0, "l": [], "r": [], "layers": []}
    fs = 8000.0
    buf = H.render_voice(layers, fs)
    n = len(buf)
    step = max(1, math.ceil(n / points))
    left, right = [], []
    for i in range(0, n, step):
        chunk = buf[i:i + step]
        left.append(round(float(abs(chunk[:, 0]).max()), 4))
        right.append(round(float(abs(chunk[:, 1]).max()), 4))
    return {"ok": True, "ms": round(n / fs * 1000), "l": left, "r": right,
            "peak": round(float(abs(buf).max()), 3),
            "layers": [{"kind": x.kind, "hz": x.hz, "ms": x.ms, "gain": x.gain, "pan": x.pan, "decay": x.decay_ms}
                       for x in layers]}


def scale_rgb(rgb, gain: float) -> list[int]:
    return [int(c) for c in T.scale_rgb(tuple(rgb), gain)]


# ---- el mando (HID, USB) -------------------------------------------------------------------------
class Pad:
    """Lee L2/R2/botones del reporte de entrada USB (0x01) y escribe gatillos y lightbar (0x02).
    Toda la E/S en un solo hilo (el de la galería): sin dos hilos sobre el mismo handle."""

    def __init__(self, say=print, demo: bool = False):
        self.say, self.demo = say, demo
        self.device = None
        self.info: dict | None = None
        self.next_try = 0.0
        self.why = "buscando mando…"
        self.l2 = self.r2 = 0
        self.buttons = 0
        self.last_input = 0.0
        self.sent = 0
        self.errors = 0

    @property
    def ok(self) -> bool:
        return self.demo or self.device is not None

    def ensure(self) -> bool:
        if self.demo or self.device is not None:
            return True
        now = time.monotonic()
        if now < self.next_try:
            return False
        self.next_try = now + 1.5
        if hid is None:
            self.why = "falta hidapi (python -m pip install hidapi)"
            return False
        controllers = find_controllers()
        usb = [c for c in controllers if c.get("bus_type") == BUS_USB and (c.get("usage_page"), c.get("usage")) in
               ((1, 5), (0, 0), (None, None))] or [c for c in controllers if c.get("bus_type") == BUS_USB]
        if not usb:
            self.why = ("DualSense por Bluetooth: conéctalo por USB" if controllers else
                        "no veo ningún DualSense por USB")
            return False
        device = hid.device()
        try:
            device.open_path(usb[0]["path"])
            device.set_nonblocking(1)
        except (OSError, IOError) as error:
            self.why = f"no puedo abrir el mando ({error})"
            return False
        self.device, self.info, self.why = device, usb[0], ""
        self.say(f"STUDIO mando {usb[0].get('model', 'DualSense')} abierto por USB")
        return True

    def drop(self, why: str) -> None:
        if self.device is not None:
            try:
                self.device.close()
            except Exception:  # noqa: BLE001
                pass
        self.device, self.why = None, why

    def poll(self, now: float) -> None:
        if self.demo:
            # gatillo simulado: R2 aprieta y suelta en ciclos, L2 a ratos
            t = now % 4.0
            self.r2 = int(255 * max(0.0, math.sin(math.pi * min(1.0, t / 1.6)))) if t < 1.6 else 0
            self.l2 = 200 if (now % 9.0) > 5.0 else 0
            self.last_input = now
            return
        if not self.ensure():
            return
        for _ in range(16):                                   # vacía lo pendiente; nos quedamos con lo último
            try:
                data = self.device.read(78)
            except (OSError, ValueError) as error:
                self.drop(f"mando desconectado ({error})")
                return
            if not data:
                break
            if data[0] == 0x01 and len(data) >= 11:
                self.l2, self.r2 = data[5], data[6]
                self.buttons = data[8] | (data[9] << 8) | (data[10] << 16)
                self.last_input = now

    def write(self, right: bytes | None, left: bytes | None, rgb) -> bool:
        if self.demo:
            self.sent += 1
            return True
        if not self.ensure():
            return False
        try:
            written = self.device.write(build_report(right, left, rgb))
        except (OSError, ValueError) as error:
            written = -1
            self.why = str(error)
        if written < 0:
            self.errors += 1
            self.drop("escritura rechazada: el mando se desconectó")
            return False
        self.sent += 1
        return True

    def square(self) -> bool:
        return bool(self.buttons & 0x10)

    def close(self) -> None:
        if self.device is not None:
            self.write(encode_effect(20, ()), encode_effect(20, ()), RELEASE_RGB)
            self.drop("cerrado")


# ---- el Studio ------------------------------------------------------------------------------------
class Studio:
    def __init__(self, here: Path, triggers_ini: Path, haptics_ini: Path, demo: bool = False,
                 haptics: bool = True, say=print):
        self.here, self.demo = here, demo
        self._said: set = set()

        def say_once(text: str) -> None:            # los avisos de los .ini se repiten en cada recarga
            if text not in self._said:
                self._said.add(text)
                say(text)
        self.say = say_once
        self.paths = {"triggers": triggers_ini, "haptics": haptics_ini}
        self.lock = threading.RLock()
        self.events: deque = deque(maxlen=40)
        self.backup_dir = here / "studio_backup"
        self.backups: dict[str, Path] = {}
        self.make_backups()
        self.tconf = T.Config(triggers_ini)
        self.hconf = H.HapticConfig(haptics_ini)
        self.tconf.load()
        self.hconf.load()
        self.pad = Pad(say, demo)
        self.out: H.HapticOutput | None = None
        self.hap_why = "desactivados"
        if haptics:
            self.start_haptics()
        thresholds = os.environ.get("UNDS_TRIGGER", "30,95").split(",")
        try:
            self.l2_press, self.r2_press = int(thresholds[0]), int(thresholds[1])
        except (ValueError, IndexError):
            self.l2_press, self.r2_press = 30, 95
        # galería de tiro
        self.range_on = True
        self.cls, self.wid = "pistola", ""
        self.ammo = CLIP["pistola"]
        self.reload_until = 0.0
        self.shots = 0
        self.prev_r2 = False
        self.prev_square = False
        self.kick: tuple = ()
        self.kick_t = 0.0
        self.auto_next = 0.0
        self.flash_until = 0.0
        self.test_effect: dict[str, tuple] = {}            # lado -> (efecto, hasta)
        self.test_rgb: tuple | None = None
        self.test_rgb_until = 0.0
        self.sent: dict = {}
        self.sent_at = 0.0
        self.game_running = False
        self.game_checked = 0.0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.loop, name="studio-io", daemon=True)

    # -- copias de seguridad --
    def make_backups(self) -> None:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self.backup_dir.mkdir(exist_ok=True)
        for kind, path in self.paths.items():
            if path.exists():
                dest = self.backup_dir / f"{path.stem}_{stamp}{path.suffix}"
                shutil.copy2(path, dest)
                self.backups[kind] = dest
        old = sorted(self.backup_dir.glob("*.ini"))
        for path in old[:-20]:                             # las 20 más recientes
            try:
                path.unlink()
            except OSError:
                pass

    def restore(self) -> dict:
        with self.lock:
            for kind, src in self.backups.items():
                shutil.copy2(src, self.paths[kind])
            self.reload()
        self.event("Ajustes restaurados a como estaban al abrir el Studio")
        return {"ok": True}

    # -- hápticos --
    def start_haptics(self) -> None:
        try:
            self.out = H.HapticOutput(self.hconf.device, say=self.say)
            self.out.set_profile(self.hconf.profile(GAME))
            self.out.start()
            self.hap_why = self.out.open_error or ""
        except Exception as error:  # noqa: BLE001 — numpy/sounddevice ausentes
            self.out, self.hap_why = None, str(error)

    def hap_status(self) -> dict:
        out = self.out
        if out is None:
            return {"ok": False, "why": self.hap_why or "sin hápticos"}
        alive = out.stream is not None and out.stream.active
        return {"ok": bool(alive), "why": "" if alive else (out.open_error or "abriendo…"), "device": out.device_name}

    # -- modelo para la interfaz --
    def event(self, text: str) -> None:
        self.events.append({"t": round(time.time(), 3), "text": text})

    def reload(self) -> None:
        self.tconf.load()
        self.hconf.load()
        if self.out is not None:
            self.out.set_profile(self.hconf.profile(GAME))

    def trigger_values(self, cls: str, wid: str = "") -> dict:
        """Valor efectivo y origen de cada clave por arma (WEAPON_KEYS + automatica)."""
        c = self.tconf
        parser = c.parser
        base = c.game_values[GAME]
        code = T.CLASS_DEFAULTS.get(cls, {})
        ini_cls = dict(parser[f"clase {cls}"]) if parser.has_section(f"clase {cls}") else {}
        ini_arma = {}
        for section in parser.sections():
            kind, _, ident = section.partition(" ")
            if kind.lower() == "arma" and wid and T.parse_weapon_id(ident) == wid:
                ini_arma = dict(parser[section])
        out = {}
        for key in (*T.WEAPON_KEYS, "automatica"):
            if key in ini_arma and key not in ("nombre", "clase"):
                value, origin = ini_arma[key], "arma"
            elif key in ini_cls:
                value, origin = ini_cls[key], "clase"
            elif key in code:
                value, origin = code[key], "clase-código"
            elif key == "automatica":
                value, origin = "auto", "código"
            else:
                value = base.get(key, T.DEFAULTS.get(key, ""))
                in_ini = (parser.has_section(GAME) and key in parser[GAME]) or \
                         (parser.has_section("default") and key in parser["default"])
                origin = "general" if in_ini else "código"
            out[key] = {"value": value.strip(), "origin": origin}
        return out

    def voice_values(self, cls: str, wid: str = "") -> dict:
        prof = self.hconf.profile(GAME)
        parser = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
        parser.read(self.paths["haptics"], encoding="utf-8")
        default = dict(H.PROFILE_DEFAULTS)
        if parser.has_section("default"):
            default.update(parser["default"])
        out = {}
        for key in H.WEAPON_VOICE_KEYS:
            if wid and parser.has_section(f"arma {wid}") and key in parser[f"arma {wid}"]:
                out[key] = {"value": parser[f"arma {wid}"][key].strip(), "origin": "arma"}
            elif parser.has_section(f"clase {cls}") and key in parser[f"clase {cls}"]:
                out[key] = {"value": parser[f"clase {cls}"][key].strip(), "origin": "clase"}
            elif key in H.CLASS_VOICES.get(cls, {}):
                out[key] = {"value": H.CLASS_VOICES[cls][key], "origin": "clase-código"}
            else:
                out[key] = {"value": default[key].strip(), "origin": "general"}
        del prof
        return out

    def haptic_globals(self) -> dict:
        parser = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
        parser.read(self.paths["haptics"], encoding="utf-8")
        values = dict(H.PROFILE_DEFAULTS)
        if parser.has_section("default"):
            values.update({k: v.strip() for k, v in parser["default"].items()})
        return values

    def model(self) -> dict:
        with self.lock:
            weapons = [{"id": wid, "name": info.name, "cls": info.cls or "pistola"}
                       for wid, info in sorted(self.tconf.weapons.items(), key=lambda kv: kv[1].name)]
            base = self.tconf.game_values[GAME]
            docs = {kind: IniDoc.read(path).sections() for kind, path in self.paths.items()}
            return {
                "version": VERSION, "game": GAME,
                "classes": list(T.CLASSES), "clip": CLIP, "weapons": weapons,
                "modes": {name: {"ranges": [list(r) for r in ranges], "labels": PARAM_LABELS.get(name, []),
                                 "help": MODE_HELP.get(name, "")}
                          for name, (_mode, ranges) in V3_MODES.items()},
                "classValues": {cls: self.trigger_values(cls) for cls in T.CLASSES},
                "weaponValues": {w["id"]: self.trigger_values(w["cls"], w["id"]) for w in weapons},
                "classVoices": {cls: self.voice_values(cls) for cls in T.CLASSES},
                "hapticGlobals": self.haptic_globals(),
                "triggerGlobals": {k: base.get(k, "").strip() for k in
                                   ("led_gain", "led_brightness", "damage_rgb", "death_rgb", "menu_rgb", "damage",
                                    "health_led", "death", "context", "combat_s", "auto_period_ms", "mounted_l2",
                                    "calm_l2", "calm_r2")},
                "thresholds": {"l2": self.l2_press, "r2": self.r2_press},
                "files": {k: str(p) for k, p in self.paths.items()},
                "backups": {k: str(p) for k, p in self.backups.items()},
                "ini": docs,
            }

    # -- escritura de ajustes --
    def set_value(self, kind: str, section: str, key: str, value: str | None) -> dict:
        """Guarda una clave (value None = quitarla: vuelve al valor heredado). Valida con el mismo código
        que el companion antes de tocar el archivo."""
        if kind not in self.paths:
            return {"ok": False, "error": f"archivo desconocido {kind!r}"}
        path = self.paths[kind]
        with self.lock:
            doc = IniDoc.read(path)
            if value is None:
                doc.unset(section, key)
            else:
                if "\n" in value or "\r" in value:
                    return {"ok": False, "error": "valor con saltos de línea"}
                doc.set(section, key, value)
            tmp = path.with_name(path.name + ".studio.tmp")
            tmp.write_text(doc.text(), encoding="utf-8")
            try:
                (T.Config if kind == "triggers" else H.HapticConfig)(tmp).load()
            except ValueError as error:
                tmp.unlink(missing_ok=True)
                return {"ok": False, "error": str(error)}
            os.replace(tmp, path)
            self.reload()
            self.sent.clear()
        shown = "por defecto" if value is None else value
        self.event(f"[{section}] {key} = {shown}")
        return {"ok": True}

    # -- pruebas --
    def test(self, req: dict) -> dict:
        kind = req.get("kind")
        now = time.monotonic()
        try:
            if kind == "trigger":
                side = "L" if req.get("side") == "L" else "R"
                effect = parse_effect(str(req["text"]))
                self.test_effect[side] = ((effect,), now + float(req.get("ms", 2500)) / 1000.0, (1.0,))
                return {"ok": True}
            if kind == "kick":
                phases = T.parse_kick(str(req["text"]), int(req.get("default_ms", 70)), "kick")
                self.test_effect["R"] = (tuple(e for e, _ in phases), now + sum(s for _, s in phases),
                                         tuple(s for _, s in phases))
                if req.get("voice"):
                    self.play_voice_text(str(req["voice"]))
                return {"ok": True}
            if kind == "voice":
                if req.get("text") is not None:
                    return self.play_voice_text(str(req["text"]), req.get("gains"))
                return self.play_voice_name(str(req["name"]))
            if kind == "hit":
                return self.play_hit(req.get("az"), req.get("amt"))
            if kind == "death":
                ok = self.play_voice_name("death")
                prof = self.tconf.profile(GAME)
                self.test_rgb = tuple(scale_rgb(prof.death_rgb, prof.led_gain))
                self.test_rgb_until = now + 3.0
                return ok
            if kind == "rumble":
                if self.out is None:
                    return {"ok": False, "error": self.hap_why}
                self.out.rumble(int(req.get("l", 0)), int(req.get("s", 0)), duckable=False)
                ms = float(req.get("ms", 0))
                if ms > 0:
                    threading.Timer(ms / 1000.0, lambda: self.out and self.out.rumble(0, 0)).start()
                return {"ok": True}
            if kind == "rgb":
                rgb = [max(0, min(255, int(c))) for c in req["rgb"]]
                self.test_rgb, self.test_rgb_until = tuple(rgb), now + float(req.get("ms", 2500)) / 1000.0
                return {"ok": True}
            if kind == "release":
                self.test_effect.clear()
                self.test_rgb = None
                if self.out is not None:
                    self.out.rumble(0, 0)
                    self.out.silence()
                return {"ok": True}
        except (KeyError, ValueError, TypeError) as error:
            return {"ok": False, "error": str(error)}
        return {"ok": False, "error": f"prueba desconocida {kind!r}"}

    def play_voice_text(self, text: str, gains=None) -> dict:
        if self.out is None:
            return {"ok": False, "error": self.hap_why or "sin hápticos"}
        layers = H.parse_voice(text)
        if not layers:
            return {"ok": True}
        buf = H.render_voice(layers, self.out.fs)
        if gains:
            buf = buf.copy()
            buf[:, 0] *= float(gains[0])
            buf[:, 1] *= float(gains[1])
        self.out.buffer(buf * self.out.device_cfg.master, "studio")
        return {"ok": True}

    def play_voice_name(self, name: str, gains=None) -> dict:
        if self.out is None:
            return {"ok": False, "error": self.hap_why or "sin hápticos"}
        return {"ok": self.out.voice(name, None, gains)}

    def play_hit(self, az, amt) -> dict:
        if self.out is None:
            return {"ok": False, "error": self.hap_why or "sin hápticos"}
        status = self.hap_status()
        prof = self.hconf.profile(GAME)
        az = None if az is None else float(az)
        amt = None if amt is None else int(amt)
        mix = H.hit_mix(prof, az, amt, lambda name: self.out.bank.get(name) is not None)
        for name, gains in mix:
            self.out.voice(name, None, gains)
        tprof = self.tconf.profile(GAME)
        if tprof.damage_on:
            self.test_rgb, self.test_rgb_until = tuple(tprof.damage_rgb), time.monotonic() + tprof.damage_flash_s
        return {"ok": True, "felt": status["ok"], "why": status.get("why", ""),
                "mix": [{"name": n, "l": round(g[0], 2), "r": round(g[1], 2)} for n, g in mix]}

    def set_range(self, req: dict) -> dict:
        with self.lock:
            if "on" in req:
                self.range_on = bool(req["on"])
            if req.get("cls") in T.CLASSES:
                self.cls, self.wid = req["cls"], str(req.get("wid") or "")
                self.ammo = CLIP.get(self.cls, 8)
                self.kick, self.auto_next = (), 0.0
                self.event(f"Galería de tiro: {self.weapon_label()}")
            if req.get("reload"):
                self.reload_until = time.monotonic() + RELOAD_S
        return {"ok": True}

    def weapon_label(self) -> str:
        info = self.tconf.weapons.get(self.wid)
        return (info.name if info and info.name else self.cls) + (f" ({self.wid})" if self.wid else "")

    # -- galería de tiro + E/S del mando --
    def profile(self):
        return self.tconf.weapon_profile(GAME, self.wid, self.cls)

    def automatic(self) -> bool:
        info = self.tconf.weapons.get(self.wid)
        if info is not None and info.automatic is not None:
            return info.automatic
        return bool(self.tconf.class_automatic(self.cls))

    def voice_for(self, kind: str) -> None:
        if self.out is None:
            return
        for name in (f"{kind}@arma:{self.wid}" if self.wid else "", f"{kind}@clase:{self.cls}", kind):
            if name and name in self.out.bank:
                self.out.voice(name)
                return

    def fire(self, now: float, p, auto: bool) -> None:
        clip = CLIP.get(self.cls, 8)
        if clip and self.ammo <= 0:
            return
        if clip:
            self.ammo -= 1
        self.shots += 1
        self.kick, self.kick_t = (p.auto_kick if auto else p.kick), now
        self.flash_until = now + 0.08
        self.voice_for("fire_auto" if auto else "fire")

    def kick_effect(self, now: float):
        t = now - self.kick_t
        for effect, seconds in self.kick:
            if t < seconds:
                return effect
            t -= seconds
        return None

    def desired(self, now: float) -> dict:
        p = self.profile()
        aiming = self.pad.l2 > self.l2_press
        firing = self.pad.r2 > self.r2_press
        square = self.pad.square()
        clip = CLIP.get(self.cls, 8)
        if self.range_on:
            if square and not self.prev_square and clip:
                self.reload_until = now + RELOAD_S
            if self.reload_until and now >= self.reload_until:
                self.ammo, self.reload_until = clip, 0.0
                self.event("Recargado")
            reloading = self.reload_until > now
            if firing and not self.prev_r2 and not reloading:
                if self.automatic():
                    self.fire(now, p, True)
                    self.auto_next = now + p.auto_period_s
                else:
                    self.fire(now, p, False)
            elif firing and self.automatic() and self.auto_next and now >= self.auto_next and not reloading:
                self.fire(now, p, True)
                self.auto_next += p.auto_period_s
            if not firing:
                self.auto_next = 0.0
        self.prev_r2, self.prev_square = firing, square
        left = p.l2
        kick = self.kick_effect(now) if self.range_on else None
        if kick is not None:
            right = kick
        elif clip and self.ammo <= 0 and self.range_on:
            right = p.empty_r2
        else:
            right = p.r2_aim if aiming else p.r2_idle
        if not self.range_on:
            left = right = parse_effect("OFF")
        for side in ("L", "R"):
            test = self.test_effect.get(side)
            if test:
                effects, until, durations = test
                if now >= until:
                    del self.test_effect[side]
                    continue
                if len(effects) == 1:
                    chosen = effects[0]
                else:
                    t = now - (until - sum(durations))
                    chosen = effects[-1]
                    for e, s in zip(effects, durations):
                        if t < s:
                            chosen = e
                            break
                        t -= s
                if side == "L":
                    left = chosen
                else:
                    right = chosen
        tprof = self.tconf.profile(GAME)
        rgb = tuple(scale_rgb((32, 32, 0), tprof.led_gain))            # color "sano" de U1
        if now < self.flash_until:
            rgb = (255, 200, 120)
        if self.test_rgb and now < self.test_rgb_until:
            rgb = self.test_rgb
        return {"L": left, "R": right, "RGB": rgb}

    def loop(self) -> None:
        while not self.stop.is_set():
            now = time.monotonic()
            if now - self.game_checked > 3.0:
                self.game_checked = now
                self.game_running = game_running()
            with self.lock:
                self.pad.poll(now)
                if self.game_running and not self.demo:
                    self.sent.clear()
                else:
                    try:
                        want = self.desired(now)
                    except ValueError as error:                 # perfil inválido a medio editar
                        self.event(f"Error: {error}")
                        want = None
                    if want is not None:
                        changed = {k: v for k, v in want.items() if self.sent.get(k) != v}
                        if changed or now - self.sent_at > REASSERT_S:
                            right = encode_effect(want["R"].mode, want["R"].params)
                            left = encode_effect(want["L"].mode, want["L"].params)
                            if self.pad.write(right, left, want["RGB"]):
                                self.sent, self.sent_at = dict(want), now
            time.sleep(TICK_S)

    def live(self) -> dict:
        with self.lock:
            now = time.monotonic()
            pad = self.pad
            return {
                "pad": {"ok": pad.ok and not self.game_running, "why": pad.why if not pad.ok else
                        ("el juego está abierto: él maneja el mando" if self.game_running else ""),
                        "demo": self.demo, "l2": pad.l2, "r2": pad.r2, "buttons": pad.buttons},
                "haptics": self.hap_status(),
                "game": self.game_running,
                "sent": {k: (str(v) if k != "RGB" else list(v)) for k, v in self.sent.items()},
                "range": {"on": self.range_on, "cls": self.cls, "wid": self.wid, "label": self.weapon_label(),
                          "ammo": self.ammo, "clip": CLIP.get(self.cls, 8), "shots": self.shots,
                          "reloading": self.reload_until > now, "aiming": pad.l2 > self.l2_press,
                          "firing": pad.r2 > self.r2_press, "flash": now < self.flash_until,
                          "auto": self.automatic()},
                "events": list(self.events)[-8:],
            }

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=1.0)
        self.pad.close()
        if self.out is not None:
            self.out.close()


def game_running() -> bool:
    if os.name != "nt":
        return False
    try:
        from unds_launcher import running
        return running("bb-probe.exe") > 0
    except Exception:  # noqa: BLE001
        return False


# ---- HTTP ---------------------------------------------------------------------------------------
def html_path() -> Path:
    for base in (Path(getattr(sys, "_MEIPASS", "")), Path(__file__).resolve().parent):
        if (base / "studio.html").is_file():
            return base / "studio.html"
    raise FileNotFoundError("studio.html")


def make_handler(studio: Studio):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"UNDS-Studio/{VERSION}"

        def log_message(self, fmt, *args):            # silencioso
            pass

        def send_json(self, obj, status=HTTPStatus.OK):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1_000_000:
                raise ValueError("cuerpo demasiado grande")
            return json.loads(self.rfile.read(length) or b"{}")

        def local_only(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            if host not in ("127.0.0.1", "localhost"):
                self.send_error(HTTPStatus.FORBIDDEN)
                return False
            return True

        def do_GET(self):
            if not self.local_only():
                return
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path in ("/", "/index.html"):
                    data = html_path().read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif url.path == "/api/model":
                    self.send_json(studio.model())
                elif url.path == "/api/live":
                    self.send_json(studio.live())
                elif url.path == "/api/effect":
                    try:
                        self.send_json(effect_zones(q.get("text", "")))
                    except ValueError as error:
                        self.send_json({"ok": False, "error": str(error)})
                elif url.path == "/api/kick":
                    try:
                        self.send_json({"ok": True, "phases": kick_phases(q.get("text", ""), int(q.get("ms", 70)))})
                    except ValueError as error:
                        self.send_json({"ok": False, "error": str(error)})
                elif url.path == "/api/voice":
                    try:
                        self.send_json(voice_wave(q.get("text", "")))
                    except ValueError as error:
                        self.send_json({"ok": False, "error": str(error)})
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            if not self.local_only():
                return
            url = urlparse(self.path)
            try:
                req = self.body()
            except (ValueError, json.JSONDecodeError) as error:
                self.send_json({"ok": False, "error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            try:
                if url.path == "/api/set":
                    value = req.get("value")
                    self.send_json(studio.set_value(str(req["file"]), str(req["section"]), str(req["key"]),
                                                    None if value is None else str(value)))
                elif url.path == "/api/test":
                    self.send_json(studio.test(req))
                elif url.path == "/api/range":
                    self.send_json(studio.set_range(req))
                elif url.path == "/api/restore":
                    self.send_json(studio.restore())
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except (KeyError, TypeError) as error:
                self.send_json({"ok": False, "error": f"petición incompleta: {error}"}, HTTPStatus.BAD_REQUEST)
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


def free_port(preferred: int) -> int:
    for port in (preferred, *range(preferred + 1, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise OSError("no hay puerto libre para el Studio")


def main(argv: list[str] | None = None, here: Path | None = None) -> int:
    here = here or (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
                    else Path(__file__).resolve().parent)
    parser = argparse.ArgumentParser(description="UNDS Studio: calibración visual de gatillos, hápticos y lightbar.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="no abrir el navegador")
    parser.add_argument("--no-haptics", action="store_true")
    parser.add_argument("--demo", action="store_true", help="sin mando: gatillos simulados")
    parser.add_argument("--triggers", default=str(here / "unds_triggers.ini"))
    parser.add_argument("--haptics", default=str(here / "unds_haptics.ini"))
    args = parser.parse_args(argv)
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    try:
        studio = Studio(here, Path(args.triggers), Path(args.haptics), demo=args.demo,
                        haptics=not args.no_haptics)
    except ValueError as error:
        print(f"[Studio] Un .ini no es válido: {error}")
        return 2
    port = free_port(args.port)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(studio))
    server.daemon_threads = True
    url = f"http://127.0.0.1:{port}/"
    studio.start()
    print(f"UNDS Studio {VERSION} en {url}")
    print("  Ajustes: " + " · ".join(str(p) for p in studio.paths.values()))
    print("  Copias de seguridad de esta sesión en " + str(studio.backup_dir))
    print("  Cierra esta ventana (o Ctrl+C) para salir: el mando queda libre.")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        studio.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
