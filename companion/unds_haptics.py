#!/usr/bin/env python3
"""UNDS — Día 4: hápticos del DualSense por WASAPI (canales 3 y 4 del endpoint de audio USB).

Por USB el DualSense es un dispositivo de audio de 4 canales (Windows: "Altavoces (Wireless
Controller)" en configuración Cuadrafónico): 1-2 = altavoz/auriculares, 3-4 = actuadores hápticos
izquierdo y derecho. Este módulo abre ese endpoint y sintetiza tres capas que se suman:

  1. Rumble del juego -> hápticos. Cada motor del DS4 se emula como un ERM (masa excéntrica): la
     amplitud sigue a la orden con la inercia del motor (ataque/relajación) y la frecuencia sube con
     la intensidad, como en un motor real. Sustituye al rumble emulado de SDL, que DEBE ir apagado
     (UNDS_RUMBLE=0): mientras SDL manda rumble, pone "Disable audio haptics" en el reporte HID
     (SDL 3.4.10, SDL_hidapi_ps5.c:733/739) y el mando ignora este audio.
  2. Voces por evento (fire = disparo propio, hit = daño recibido, death = muerte): ondas cortas
     prediseñadas en el .ini. Durante el disparo la capa 1 se silencia (fire_duck_ms) para no duplicar
     el rumble del arma. Día 6: "hit" se reparte entre los actuadores según el ángulo de la fuente del
     daño (evento dmg: 0 = delante, + = derecha) y su fuerza sigue a la cantidad de daño; mientras
     suena, el rumble de daño del juego baja a hit_duck. Durante la muerte el rumble baja a death_duck
     hasta la reaparición, para que la voz "death" tenga todo el margen.
  3. Opcional: audio -> hápticos a partir del nivel de graves (lf) del puerto main que publica el
     port (no hay PCM en el bus, solo la envolvente cada 10,67 ms).

Herramientas (sin juego):
    python unds_haptics.py --list                 # salidas de audio; marca el DualSense con 4 canales
    python unds_haptics.py --test                 # pulsos: izquierdo, derecho, ambos
    python unds_haptics.py --sweep                # barrido 20-400 Hz para calibrar las frecuencias
    python unds_haptics.py --play fire            # una voz del .ini (fire, fire_auto, hit, death)
    python unds_haptics.py --play hit --azimuth 90 --amt 90   # golpe desde la derecha, bala de rifle
    python unds_haptics.py --hit-demo             # golpes desde izquierda, derecha, delante, detrás y 360°
    python unds_haptics.py --rumble 76,255,180    # traducción de un rumble: motor grande, pequeño, ms
En juego lo usa unds.py (gatillos + hápticos en un solo proceso).

Requiere numpy y sounddevice:  python -m pip install --upgrade numpy sounddevice
"""
from __future__ import annotations

import argparse
import configparser
import math
import os
import sys
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field, replace
from pathlib import Path

try:
    import numpy as np
except ImportError as _error:            # se informa al abrir, no al importar (unds.py --no-haptics)
    np = None  # type: ignore[assignment]
    NUMPY_ERROR: Exception | None = _error
else:
    NUMPY_ERROR = None
try:
    import sounddevice as sd
except (ImportError, OSError) as _error:
    sd = None  # type: ignore[assignment]
    SD_ERROR: Exception | None = _error
else:
    SD_ERROR = None

TWO_PI = 2.0 * math.pi
LIMIT_KNEE = 0.8                 # el limitador empieza a comprimir aquí (fondo de escala = 1)
NAME_HINTS = ("wireless controller", "dualsense")
VOICE_NAMES = ("fire", "fire_auto", "hit", "hit_front", "hit_back", "death")
DEVICE_DEFAULTS = {
    "name": "auto",
    "exclusive": "no",
    "latency": "low",
    "channels": "3,4",
    "master": "1.0",
    "align_ms": "0",
}
PROFILE_DEFAULTS = {
    "rumble": "on",
    "rumble_l_hz": "35-60",
    "rumble_s_hz": "130-190",
    "rumble_l_gain": "0.7",
    "rumble_s_gain": "0.5",
    "rumble_l_pan": "-0.3",
    "rumble_s_pan": "0.3",
    "rumble_curve": "0.8",
    "rumble_attack_ms": "15",
    "rumble_release_ms": "45",
    "fire": "SINE 65 110 0.8 0.6 40 + NOISE 300 30 0.35 0.6 8",
    "fire_duck_ms": "190",
    "fire_auto": "SINE 65 60 0.85 0.6 22 + NOISE 300 18 0.35 0.6 6",
    "hit": "SINE 70 150 1.0 0 55 + NOISE 220 60 0.55 0 15",
    "hit_front": "SINE 160 90 0.9 0 30 + NOISE 600 40 0.6 0 10",
    "hit_back": "SINE 45 230 1.0 0 90 + SINE 70 120 0.45 0 40",
    "hit_timbre": "on",
    "hit_timbre_curve": "1.0",
    "hit_direction": "on",
    "hit_pan": "1.0",
    "hit_pan_curve": "0.6",
    "hit_gain_min": "0.6",
    "hit_full_amt": "90",
    "hit_source": "state",
    "hit_duck_ms": "300",
    "hit_duck": "0.2",
    "death": "SINE 60 250 0.5 0 90 + SINE 40 6000 0.9 0 1700",
    "death_stop_ms": "150",
    "death_duck": "0.0",
    "death_duck_max_ms": "8000",
    "audio": "off",
    "audio_hz": "55",
    "audio_gain": "0.6",
    "audio_pan": "0",
    "audio_floor_db": "-45",
    "audio_margin_db": "6",
    "audio_range_db": "12",
    "audio_slow_ms": "400",
    "audio_attack_ms": "3",
    "audio_release_ms": "60",
}
GAMES = ("default", "u1", "u2", "u3", "other")
# Día 7: voz de disparo por clase de arma (las clases las decide unds_triggers.py). Sobre fire/fire_auto
# del perfil; [clase X] y [arma 0x...] del .ini pueden cambiarlas (solo las claves fire y fire_auto).
WEAPON_CLASSES = ("pistola", "revolver", "subfusil", "automatica", "escopeta", "lanzagranadas", "torreta", "granada")
WEAPON_VOICE_KEYS = ("fire", "fire_auto")
CLASS_VOICES: dict[str, dict[str, str]] = {
    "revolver": {"fire": "SINE 55 140 0.9 0.2 50 + NOISE 250 40 0.4 0.2 10"},          # más grave y largo
    "subfusil": {"fire_auto": "SINE 75 45 0.6 0.3 15 + NOISE 350 15 0.3 0.3 5"},       # más ligero y rápido
    "escopeta": {"fire": "SINE 50 180 1.0 0 70 + NOISE 220 70 0.6 0 20"},              # golpe ancho, centrado
    "lanzagranadas": {"fire": "SINE 45 260 1.0 0 110 + SINE 90 80 0.4 0 25"},          # empujón sordo y largo
    "torreta": {"fire_auto": "SINE 55 70 0.95 0 25 + NOISE 250 25 0.45 0 8"},          # pesada, centrada
    "granada": {"fire": "OFF"},
}


class HapticError(RuntimeError):
    """Error con un mensaje listo para el usuario."""


# ---- configuración --------------------------------------------------------------------------------
def parse_bool(text: str) -> bool:
    value = text.strip().lower()
    if value in ("1", "yes", "si", "sí", "on", "true"):
        return True
    if value in ("0", "no", "off", "false"):
        return False
    raise ValueError(f"{text!r}: se espera on/off")


def parse_float(text: str, low: float, high: float, what: str) -> float:
    value = float(text)
    if not low <= value <= high:
        raise ValueError(f"{what} = {value}: fuera de [{low}, {high}]")
    return value


def parse_range(text: str, what: str) -> tuple[float, float]:
    """'35-60' o '50' (fijo) en Hz."""
    parts = text.replace(" ", "").split("-")
    if len(parts) == 1:
        lo = hi = parse_float(parts[0], 1, 1000, what)
    elif len(parts) == 2:
        lo, hi = parse_float(parts[0], 1, 1000, what), parse_float(parts[1], 1, 1000, what)
    else:
        raise ValueError(f"{what} = {text!r}: se espera 'min-max' en Hz")
    return lo, hi


@dataclass(frozen=True)
class Layer:
    kind: str        # SINE | NOISE
    hz: float        # SINE: frecuencia; NOISE: corte del paso bajo
    ms: float
    gain: float
    pan: float       # -1 izquierdo ... 0 ambos ... +1 derecho
    decay_ms: float  # constante de caída exponencial; 0 = plana


def parse_voice(text: str) -> tuple[Layer, ...]:
    """'SINE hz ms ganancia pan caída_ms [+ NOISE corte_hz ms ganancia pan caída_ms ...]' u 'OFF'."""
    text = text.strip()
    if text.upper() in ("", "OFF", "NONE"):
        return ()
    layers = []
    for chunk in text.split("+"):
        words = chunk.split()
        if len(words) != 6 or words[0].upper() not in ("SINE", "NOISE"):
            raise ValueError(f"capa {chunk.strip()!r}: se espera 'SINE|NOISE hz ms ganancia pan caída_ms'")
        kind = words[0].upper()
        layers.append(Layer(
            kind=kind,
            hz=parse_float(words[1], 1, 4000 if kind == "NOISE" else 1000, "hz"),
            ms=parse_float(words[2], 1, 10000, "ms"),
            gain=parse_float(words[3], 0, 2, "ganancia"),
            pan=parse_float(words[4], -1, 1, "pan"),
            decay_ms=parse_float(words[5], 0, 10000, "caída_ms")))
    return tuple(layers)


def pan_gains(pan: float) -> tuple[float, float]:
    """Centro = ambos a 1; los extremos apagan el lado opuesto (no reduce el centro)."""
    return min(1.0, 1.0 - pan), min(1.0, 1.0 + pan)


def hit_gains(profile: "HapticProfile", azimuth: float | None, amount: int | None) -> tuple[float, float]:
    """Ganancias (izquierda, derecha) de la voz hit para un golpe.

    Lado: pan = hit_pan * signo(s) * |s|^hit_pan_curve con s = sen(ángulo) (0 = delante, + = derecha,
    del cuerpo de Drake; VERIFICADO en la sesión 145452). Delante y detrás (s ~ 0) caen en ambos
    actuadores; con la curva < 1 un golpe en diagonal ya se va claramente a su lado. Sin ángulo (daño
    sin fuente, tipo 3) o hit_direction = off: centrado.
    Fuerza: hit_gain_min con un golpe mínimo, 1 a partir de hit_full_amt milésimas de vida (U1: una
    bala de rifle quita 90-92, un perdigón 13-21)."""
    pan = 0.0
    if profile.hit_direction and azimuth is not None:
        side = math.sin(math.radians(azimuth))
        pan = profile.hit_pan * math.copysign(abs(side) ** profile.hit_pan_curve, side)
    gain = 1.0
    if amount is not None and amount >= 0:
        k = min(1.0, amount / profile.hit_full_amt)
        gain = profile.hit_gain_min + (1.0 - profile.hit_gain_min) * k
    left, right = pan_gains(pan)
    return left * gain, right * gain


def hit_mix(profile: "HapticProfile", azimuth: float | None, amount: int | None,
            available=lambda name: True) -> list[tuple[str, tuple[float, float]]]:
    """Voces de un golpe con sus ganancias (izquierda, derecha): [(nombre, (gi, gd)), ...].

    El lado lo pone hit_gains() (paneo + fuerza). El timbre, el eje delante-detrás, que dos actuadores
    no pueden dar por posición: c = cos(ángulo); peso de hit_front = max(0, c)^curva, de hit_back =
    max(0, -c)^curva y de hit (golpe de lado) el resto. Delante puro = solo agudo, detrás puro = solo
    grave, 90° = solo hit; 45° = 71 % agudo + 29 % hit, todo cargado a su lado. Sin ángulo, con
    hit_timbre = off o con hit_front / hit_back en OFF, su peso pasa a hit."""
    left, right = hit_gains(profile, azimuth, amount)
    front = back = 0.0
    if profile.hit_timbre and profile.hit_direction and azimuth is not None:
        c = math.cos(math.radians(azimuth))
        if c > 0 and available("hit_front"):
            front = c ** profile.hit_timbre_curve
        elif c < 0 and available("hit_back"):
            back = (-c) ** profile.hit_timbre_curve
    side = max(0.0, 1.0 - front - back)
    mix = []
    for name, weight in (("hit_front", front), ("hit_back", back), ("hit", side)):
        if weight >= 0.02:                      # menos no se siente: no se dispara la voz
            mix.append((name, (left * weight, right * weight)))
    return mix


@dataclass(frozen=True)
class DeviceConfig:
    name: str
    exclusive: bool
    latency: str | float
    channels: tuple[int, int]      # índices 0-based (izquierdo, derecho)
    master: float
    align_s: float


@dataclass(frozen=True)
class HapticProfile:
    rumble: bool
    l_hz: tuple[float, float]
    s_hz: tuple[float, float]
    l_gain: float
    s_gain: float
    l_pan: float
    s_pan: float
    curve: float
    attack_s: float
    release_s: float
    voices: dict                    # nombre -> tuple[Layer, ...]
    fire_duck_s: float
    death_stop_s: float
    hit_direction: bool
    hit_timbre: bool                # delante agudo (hit_front), detrás grave (hit_back), lados = hit
    hit_timbre_curve: float
    hit_pan: float
    hit_pan_curve: float
    hit_gain_min: float
    hit_full_amt: float
    hit_source: str                 # state (solo evento dmg) | any (también la firma de rumble)
    hit_duck_s: float
    hit_duck: float
    death_duck: float
    death_duck_max_s: float
    audio: str                      # off | transient | level
    audio_hz: float
    audio_gain: float
    audio_pan: float
    audio_floor_db: float
    audio_margin_db: float
    audio_range_db: float
    audio_slow_s: float
    audio_attack_s: float
    audio_release_s: float
    weapon_voices: dict = field(default_factory=dict)   # "fire@clase:escopeta" / "fire@arma:0x..." -> capas


def build_profile(values: dict[str, str]) -> HapticProfile:
    audio = values["audio"].strip().lower()
    if audio not in ("off", "transient", "level"):
        raise ValueError(f"audio = {audio!r}: off | transient | level")
    hit_source = values["hit_source"].strip().lower()
    if hit_source not in ("state", "any"):
        raise ValueError(f"hit_source = {hit_source!r}: state | any")
    return HapticProfile(
        rumble=parse_bool(values["rumble"]),
        l_hz=parse_range(values["rumble_l_hz"], "rumble_l_hz"),
        s_hz=parse_range(values["rumble_s_hz"], "rumble_s_hz"),
        l_gain=parse_float(values["rumble_l_gain"], 0, 2, "rumble_l_gain"),
        s_gain=parse_float(values["rumble_s_gain"], 0, 2, "rumble_s_gain"),
        l_pan=parse_float(values["rumble_l_pan"], -1, 1, "rumble_l_pan"),
        s_pan=parse_float(values["rumble_s_pan"], -1, 1, "rumble_s_pan"),
        curve=parse_float(values["rumble_curve"], 0.1, 4, "rumble_curve"),
        attack_s=parse_float(values["rumble_attack_ms"], 0, 1000, "rumble_attack_ms") / 1000.0,
        release_s=parse_float(values["rumble_release_ms"], 0, 2000, "rumble_release_ms") / 1000.0,
        voices={name: parse_voice(values[name]) for name in VOICE_NAMES},
        fire_duck_s=parse_float(values["fire_duck_ms"], 0, 2000, "fire_duck_ms") / 1000.0,
        death_stop_s=parse_float(values["death_stop_ms"], 0, 5000, "death_stop_ms") / 1000.0,
        hit_direction=parse_bool(values["hit_direction"]),
        hit_timbre=parse_bool(values["hit_timbre"]),
        hit_timbre_curve=parse_float(values["hit_timbre_curve"], 0.1, 4, "hit_timbre_curve"),
        hit_pan=parse_float(values["hit_pan"], 0, 1, "hit_pan"),
        hit_pan_curve=parse_float(values["hit_pan_curve"], 0.1, 3, "hit_pan_curve"),
        hit_gain_min=parse_float(values["hit_gain_min"], 0, 1, "hit_gain_min"),
        hit_full_amt=parse_float(values["hit_full_amt"], 1, 1000, "hit_full_amt"),
        hit_source=hit_source,
        hit_duck_s=parse_float(values["hit_duck_ms"], 0, 2000, "hit_duck_ms") / 1000.0,
        hit_duck=parse_float(values["hit_duck"], 0, 1, "hit_duck"),
        death_duck=parse_float(values["death_duck"], 0, 1, "death_duck"),
        death_duck_max_s=parse_float(values["death_duck_max_ms"], 0, 60000, "death_duck_max_ms") / 1000.0,
        audio=audio,
        audio_hz=parse_float(values["audio_hz"], 1, 1000, "audio_hz"),
        audio_gain=parse_float(values["audio_gain"], 0, 2, "audio_gain"),
        audio_pan=parse_float(values["audio_pan"], -1, 1, "audio_pan"),
        audio_floor_db=parse_float(values["audio_floor_db"], -120, 0, "audio_floor_db"),
        audio_margin_db=parse_float(values["audio_margin_db"], 0, 60, "audio_margin_db"),
        audio_range_db=parse_float(values["audio_range_db"], 0.5, 60, "audio_range_db"),
        audio_slow_s=parse_float(values["audio_slow_ms"], 10, 10000, "audio_slow_ms") / 1000.0,
        audio_attack_s=parse_float(values["audio_attack_ms"], 0, 1000, "audio_attack_ms") / 1000.0,
        audio_release_s=parse_float(values["audio_release_ms"], 0, 2000, "audio_release_ms") / 1000.0)


class HapticConfig:
    """unds_haptics.ini: [device] + [default] + [u1]/[u2]/[u3]. Se recarga al guardar (unds.py)."""

    def __init__(self, path: Path):
        self.path = path
        self.mtime = 0.0
        self.device: DeviceConfig | None = None
        self.profiles: dict[str, HapticProfile] = {}

    def load(self) -> None:
        parser = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
        if self.path.exists():
            parser.read(self.path, encoding="utf-8")
        dev = dict(DEVICE_DEFAULTS)
        if parser.has_section("device"):
            dev.update(parser["device"])
        try:
            chans = [int(c) for c in dev["channels"].replace(" ", "").split(",")]
            if len(chans) != 2 or min(chans) < 1:
                raise ValueError(f"channels = {dev['channels']!r}: dos canales 1-based, p. ej. 3,4")
            latency_text = dev["latency"].strip().lower()
            latency: str | float = latency_text if latency_text in ("low", "high") else \
                parse_float(latency_text, 1, 500, "latency (ms)") / 1000.0
            device = DeviceConfig(
                name=dev["name"].strip(), exclusive=parse_bool(dev["exclusive"]), latency=latency,
                channels=(chans[0] - 1, chans[1] - 1),
                master=parse_float(dev["master"], 0, 2, "master"),
                align_s=parse_float(dev["align_ms"], 0, 200, "align_ms") / 1000.0)
        except ValueError as error:
            raise ValueError(f"[device] {error}") from None
        default = dict(PROFILE_DEFAULTS)
        if parser.has_section("default"):
            default.update(parser["default"])
        profiles = {}
        for game in GAMES:
            values = dict(default)
            if game != "default" and parser.has_section(game):
                values.update(parser[game])
            try:
                profiles[game] = build_profile(values)
            except (KeyError, ValueError) as error:
                raise ValueError(f"[{game}] {error}") from None
        weapon_voices = self.load_weapon_voices(parser)
        profiles = {game: replace(prof, weapon_voices=weapon_voices) for game, prof in profiles.items()}
        self.device, self.profiles = device, profiles
        self.mtime = self.path.stat().st_mtime if self.path.exists() else 0.0

    @staticmethod
    def load_weapon_voices(parser: configparser.ConfigParser) -> dict:
        """Voces por clase (código + [clase X]) y por arma ([arma 0x...]); solo fire / fire_auto."""
        texts: dict[str, str] = {}
        for cls, voices in CLASS_VOICES.items():
            for name, text in voices.items():
                texts[f"{name}@clase:{cls}"] = text
        for section in parser.sections():
            kind, _, ident = section.strip().partition(" ")
            kind = kind.lower()
            if kind not in ("clase", "arma"):
                continue
            items = dict(parser[section])
            unknown = sorted(set(items) - set(WEAPON_VOICE_KEYS))
            if unknown:
                raise ValueError(f"[{section}] clave(s) {', '.join(unknown)}: aquí solo fire y fire_auto")
            if kind == "clase":
                key = ident.strip().lower()
                if key not in WEAPON_CLASSES:
                    raise ValueError(f"[{section}]: clase desconocida; clases: {', '.join(WEAPON_CLASSES)}")
                key = f"clase:{key}"
            else:
                try:
                    key = f"arma:0x{int(ident.strip(), 16):x}"
                except ValueError:
                    raise ValueError(f"[{section}]: se espera [arma 0x58d20069]") from None
            for name, text in items.items():
                texts[f"{name}@{key}"] = text
        voices = {}
        for key, text in texts.items():
            try:
                voices[key] = parse_voice(text)
            except ValueError as error:
                raise ValueError(f"{key.split('@')[0]} de {key.split('@')[1]}: {error}") from None
        return voices

    def changed(self) -> bool:
        try:
            return self.path.stat().st_mtime != self.mtime
        except OSError:
            return False

    def profile(self, game: str) -> HapticProfile:
        return self.profiles.get(game) or self.profiles["default"]


# ---- síntesis -------------------------------------------------------------------------------------
def render_voice(layers: tuple[Layer, ...], fs: float, seed: int = 1234):
    """Capas -> buffer float32 (n, 2) [izquierdo, derecho]. Arranca y termina en 0 (sin escalón)."""
    if not layers:
        return None
    n = max(int(round(layer.ms * fs / 1000.0)) for layer in layers)
    out = np.zeros((n, 2), dtype=np.float64)
    rng = np.random.default_rng(seed)
    for layer in layers:
        m = max(2, int(round(layer.ms * fs / 1000.0)))
        t = np.arange(m) / fs
        env = np.exp(-t / (layer.decay_ms / 1000.0)) if layer.decay_ms > 0 else np.ones(m)
        attack = min(m, max(1, int(0.001 * fs)))
        env[:attack] *= np.arange(attack) / attack
        release = min(m, max(1, int(0.004 * fs)))
        env[m - release:] *= 0.5 * (1.0 + np.cos(np.linspace(0.0, math.pi, release)))
        if layer.kind == "SINE":
            wave = np.sin(TWO_PI * layer.hz * t)
        else:
            # Ruido blanco con paso bajo de fase cero (magnitud Butterworth de orden 2) por FFT.
            spectrum = np.fft.rfft(rng.standard_normal(m))
            freqs = np.fft.rfftfreq(m, 1.0 / fs)
            spectrum /= np.sqrt(1.0 + (freqs / layer.hz) ** 4)
            spectrum[0] = 0.0                                   # sin continua
            wave = np.fft.irfft(spectrum, m)
            peak = np.max(np.abs(wave))
            wave = wave / peak if peak > 0 else wave
        left, right = pan_gains(layer.pan)
        sig = layer.gain * env * wave
        out[:m, 0] += left * sig
        out[:m, 1] += right * sig
    return out.astype(np.float32)


class _Osc:
    __slots__ = ("env", "phase")

    def __init__(self) -> None:
        self.env = 0.0
        self.phase = 0.0


def device_list() -> list[tuple[int, dict, str]]:
    hostapis = sd.query_hostapis()
    return [(i, d, hostapis[d["hostapi"]]["name"]) for i, d in enumerate(sd.query_devices())
            if d["max_output_channels"] > 0]


def find_device(name: str = "auto") -> tuple[int, dict]:
    """Endpoint WASAPI del DualSense con >= 4 canales. HapticError con el remedio si no está."""
    hints = NAME_HINTS if name.lower() == "auto" else (name.lower(),)
    outputs = device_list()
    wasapi = [(i, d) for i, d, api in outputs if "wasapi" in api.lower()]
    if not any("wasapi" in api.lower() for _, _, api in outputs):
        raise HapticError("PortAudio no ofrece WASAPI (¿sounddevice sin la DLL de Windows?).")
    matches = [(i, d) for i, d in wasapi if any(h in d["name"].lower() for h in hints)]
    if not matches:
        raise HapticError(f"no hay ninguna salida WASAPI que contenga {' / '.join(repr(h) for h in hints)}. "
                          f"¿DualSense por USB (por Bluetooth no hay audio)? Mira 'python unds_haptics.py --list'.")
    four = [(i, d) for i, d in matches if d["max_output_channels"] >= 4]
    if not four:
        i, d = matches[0]
        raise HapticError(
            f"'{d['name']}' está configurado con {d['max_output_channels']} canales: los hápticos son los canales "
            f"3 y 4. Panel de control > Sonido > Reproducción > ese dispositivo > Configurar > Cuadrafónico.")
    return four[0]


class HapticOutput:
    """Stream WASAPI con callback. Los métodos públicos se llaman desde cualquier hilo: solo encolan
    órdenes (deque, atómica en CPython); el estado de síntesis solo lo toca el hilo de audio."""

    def __init__(self, device: DeviceConfig, say=print):
        self.device_cfg = device
        self.say = say
        self.cmds: deque = deque()
        self.stream = None
        self.device_name = "-"
        self.fs = 48000.0
        self.nch = 4
        self.ch_l, self.ch_r = device.channels
        self.profile: HapticProfile | None = None
        self.bank: dict = {}                    # nombre -> buffer renderizado a self.fs
        self.exclusive = device.exclusive
        # estado de síntesis (hilo de audio)
        self.clock = 0
        self.osc = (_Osc(), _Osc(), _Osc())     # motor grande, motor pequeño, audio
        self.targets = [0.0, 0.0, 0.0]
        self.duck_until = 0                     # disparo: rumble a 0 (si es "duckable")
        self.hit_duck_until = 0                 # daño: rumble a hit_duck (si es "duckable")
        self.mute_until = 0                     # muerte: rumble a death_duck (todo, también explosiones)
        self.rumble_duckable = True
        self.voices: list[list] = []            # [buffer, inicio, nombre, fundido restante, total, (gan_i, gan_d)]
        self.pow_cache: dict = {}
        # audio -> hápticos (hilo del listener)
        self.audio_slow_db: float | None = None
        # estadísticas (enteros: el GIL basta)
        self.counts: Counter = Counter()
        self.latencies: deque = deque(maxlen=4000)   # ms evento -> DAC (estimación PortAudio)
        self.reports: deque = deque()                # para el JSONL (los vacía el companion)
        self.last_error: str | None = None
        self.open_error: str | None = None
        self.stop = threading.Event()
        self.watchdog: threading.Thread | None = None
        self.open_lock = threading.Lock()

    # ---- API de eventos (cualquier hilo) ---------------------------------------------------------
    def set_profile(self, profile: HapticProfile) -> None:
        bank = {name: render_voice(layers, self.fs) for name, layers in profile.voices.items()}
        bank.update({name: render_voice(layers, self.fs) for name, layers in profile.weapon_voices.items()})
        for name, buf in bank.items():
            peak = float(np.max(np.abs(buf))) * self.device_cfg.master if buf is not None else 0.0
            if peak > 1.35:              # hasta ~1,3 el limitador suave lo absorbe bien
                self.say(f"HAPT  AVISO: la voz {name} llega a {peak:.2f} de fondo de escala y pasará por el limitador; "
                         f"baja sus ganancias")
        self.bank = bank
        self.profile = profile
        self.audio_slow_db = None

    def set_device_config(self, device: DeviceConfig) -> bool:
        """Devuelve True si hace falta reabrir (cambió el dispositivo, modo, canales o latencia)."""
        old, self.device_cfg = self.device_cfg, device
        return (old.name, old.exclusive, old.latency, old.channels) != \
               (device.name, device.exclusive, device.latency, device.channels)

    def rumble(self, large: int, small: int, duckable: bool = True) -> None:
        """duckable=False: este rumble no es la firma del arma (p. ej. una explosión) y no se silencia
        aunque coincida con un disparo."""
        self.cmds.append(("rumble", max(0, min(255, large)) / 255.0, max(0, min(255, small)) / 255.0, duckable))

    def voice(self, name: str, t_event: float | None = None, gains: tuple[float, float] | None = None) -> bool:
        """gains: (izquierda, derecha) que multiplican la voz ya renderizada (p. ej. hit direccional)."""
        if self.bank.get(name) is None:
            return False
        self.counts[f"voz_{name.split('@')[0]}"] += 1
        self.cmds.append(("voice", name, time.perf_counter() if t_event is None else t_event, gains))
        return True

    def mute_rumble(self, on: bool) -> None:
        """Muerte: el rumble del juego baja a death_duck hasta mute_rumble(False) (o death_duck_max_ms)."""
        self.cmds.append(("mute", on))

    def stop_voice(self, name: str, fade_s: float) -> None:
        """Apaga una voz en curso con un fundido lineal de fade_s (p. ej. death al reaparecer)."""
        self.cmds.append(("stop", name, max(0.001, fade_s)))

    def buffer(self, buf, tag: str = "test") -> None:
        """Reproduce un buffer (n, 2) tal cual (herramientas)."""
        self.cmds.append(("buffer", buf, tag))

    def audio_level(self, lf: int, frames: int, fs_port: float = 48000.0) -> None:
        """Nivel de graves (lf, x10000 de fondo de escala) de una ventana del puerto main."""
        p = self.profile
        if p is None or p.audio == "off":
            return
        db = 20.0 * math.log10(lf / 10000.0) if lf > 0 else -120.0
        if p.audio == "level":
            amp = (db - p.audio_floor_db) / p.audio_range_db
        else:
            slow = db if self.audio_slow_db is None else self.audio_slow_db
            amp = (db - slow - p.audio_margin_db) / p.audio_range_db if db > p.audio_floor_db else 0.0
            k = 1.0 - math.exp(-(frames / fs_port) / p.audio_slow_s)
            self.audio_slow_db = slow + k * (db - slow)
        self.cmds.append(("audio", max(0.0, min(1.0, amp))))

    def silence(self) -> None:
        self.cmds.append(("silence",))

    # ---- hilo de audio ---------------------------------------------------------------------------
    def _approach(self, start: float, target: float, tau: float, n: int):
        """Filtro de un polo hacia un objetivo constante, en forma cerrada (vectorizado)."""
        if tau <= 0.0:
            return np.full(n, target)
        a = math.exp(-1.0 / (tau * self.fs))
        key = (a, n)
        powers = self.pow_cache.get(key)
        if powers is None:
            if len(self.pow_cache) > 64:
                self.pow_cache.clear()
            powers = self.pow_cache[key] = a ** np.arange(1, n + 1)
        return target + (start - target) * powers

    def _drain(self, now: float, tinfo) -> None:
        dac = 0.0
        try:
            if tinfo.outputBufferDacTime > 0 and tinfo.currentTime > 0:
                dac = tinfo.outputBufferDacTime - tinfo.currentTime
        except AttributeError:
            pass
        if dac <= 0.0 and self.stream is not None:
            dac = float(self.stream.latency or 0.0)
        while self.cmds:
            try:
                cmd = self.cmds.popleft()
            except IndexError:
                break
            kind = cmd[0]
            if kind == "rumble":
                self.targets[0], self.targets[1], self.rumble_duckable = cmd[1], cmd[2], cmd[3]
            elif kind == "audio":
                self.targets[2] = cmd[1]
            elif kind == "voice":
                buf = self.bank.get(cmd[1])
                if buf is None:
                    continue
                start = self.clock + int(round(self.device_cfg.align_s * self.fs))
                self.voices = [v for v in self.voices if v[2] != cmd[1]]   # la misma voz no se apila
                self.voices.append([buf, start, cmd[1], None, None, cmd[3]])
                p = self.profile
                if cmd[1].startswith("fire") and p is not None:
                    self.duck_until = max(self.duck_until, start + int(p.fire_duck_s * self.fs))
                elif cmd[1].startswith("hit") and p is not None:
                    self.hit_duck_until = max(self.hit_duck_until, start + int(p.hit_duck_s * self.fs))
                queue_ms = (now - cmd[2]) * 1000.0
                total_ms = queue_ms + (dac + (start - self.clock) / self.fs) * 1000.0
                self.latencies.append(total_ms)
                self.reports.append({"voz": cmd[1], "cola_ms": round(queue_ms, 2), "e2dac_ms": round(total_ms, 2)})
            elif kind == "buffer":
                self.voices.append([cmd[1], self.clock, cmd[2], None, None, None])
            elif kind == "mute":
                p = self.profile
                limit = p.death_duck_max_s if p is not None else 8.0
                self.mute_until = self.clock + int(limit * self.fs) if cmd[1] else 0
            elif kind == "stop":
                fade = max(1, int(cmd[2] * self.fs))
                for v in self.voices:
                    if v[2] == cmd[1] and v[3] is None:
                        v[3] = v[4] = fade
            elif kind == "silence":
                self.targets = [0.0, 0.0, 0.0]
                self.voices.clear()
                self.duck_until = self.hit_duck_until = self.mute_until = 0

    def _render(self, frames: int):
        p = self.profile
        out = np.zeros((frames, 2))
        active = False
        if p is not None:
            # Atenuación del rumble del juego (el objetivo baja; la inercia del ERM suaviza el cambio):
            # disparo -> 0, daño -> hit_duck (solo rumbles con forma de disparo/daño), muerte -> death_duck.
            duck = 1.0
            if self.rumble_duckable:
                if self.clock < self.duck_until:
                    duck = 0.0
                elif self.clock < self.hit_duck_until:
                    duck = p.hit_duck
            if self.clock < self.mute_until:
                duck = min(duck, p.death_duck)
            layers = (
                (p.l_hz, p.l_gain, p.l_pan, p.curve, p.attack_s, p.release_s, p.rumble, duck),
                (p.s_hz, p.s_gain, p.s_pan, p.curve, p.attack_s, p.release_s, p.rumble, duck),
                ((p.audio_hz, p.audio_hz), p.audio_gain, p.audio_pan, 1.0, p.audio_attack_s, p.audio_release_s,
                 p.audio != "off", 1.0 if self.clock >= self.mute_until else p.death_duck),
            )
            for osc, target, (hz, gain, pan, curve, attack, release, enabled, k) in zip(self.osc, self.targets, layers):
                target = target * k if enabled else 0.0
                if target < 1e-4 and osc.env < 1e-4:
                    osc.env = 0.0
                    continue
                env = self._approach(osc.env, target, attack if target > osc.env else release, frames)
                osc.env = float(env[-1])
                lo, hi = hz
                freq = lo + (hi - lo) * env if hi != lo else np.full(frames, lo)
                phase = osc.phase + TWO_PI * np.cumsum(freq) / self.fs
                osc.phase = float(phase[-1] % TWO_PI)
                sig = gain * np.power(env, curve) * np.sin(phase)
                left, right = pan_gains(pan)
                out[:, 0] += left * sig
                out[:, 1] += right * sig
                active = True
        if self.voices:
            keep = []
            for voice in self.voices:
                buf, start = voice[0], voice[1]
                offset = start - self.clock                  # dónde empieza dentro de este bloque
                if offset >= frames:
                    keep.append(voice)
                    continue
                s0 = max(0, offset)
                b0 = s0 - offset
                n = min(frames - s0, len(buf) - b0)
                fade_left, fade_total = voice[3], voice[4]
                if fade_left is not None:
                    n = min(n, fade_left)                    # fundido a cero y fin de la voz
                if n > 0:
                    chunk = buf[b0:b0 + n]
                    if voice[5] is not None:
                        chunk = chunk * np.asarray(voice[5], dtype=np.float32)
                    if fade_left is not None:
                        ramp = (fade_left - np.arange(n)) / fade_total
                        chunk = chunk * ramp[:, None]
                        voice[3] = fade_left - n
                    out[s0:s0 + n] += chunk
                    active = True
                if b0 + n < len(buf) and (voice[3] is None or voice[3] > 0):
                    keep.append(voice)
            self.voices = keep
        if not active:
            return None
        out *= self.device_cfg.master
        # Limitador suave: lineal hasta LIMIT_KNEE y tanh por encima (pendiente continua, nunca pasa de 1).
        mag = np.abs(out)
        over = mag > LIMIT_KNEE
        if over.any():
            self.counts["limitador"] += 1
            m = mag[over]
            out[over] = np.sign(out[over]) * (LIMIT_KNEE + (1.0 - LIMIT_KNEE) *
                                              np.tanh((m - LIMIT_KNEE) / (1.0 - LIMIT_KNEE)))
        return out

    def _callback(self, outdata, frames, tinfo, status) -> None:
        try:
            if status.output_underflow:
                self.counts["underflow"] += 1
            self._drain(time.perf_counter(), tinfo)
            hap = self._render(frames)
            outdata.fill(0.0)
            if hap is not None:
                outdata[:, self.ch_l] = hap[:, 0]
                outdata[:, self.ch_r] = hap[:, 1]
            self.clock += frames
            self.counts["bloques"] += 1
        except Exception as error:  # noqa: BLE001 — nunca propagar al hilo de PortAudio
            outdata.fill(0.0)
            self.counts["errores_callback"] += 1
            self.last_error = repr(error)

    def _finished(self) -> None:
        self.counts["stream_terminado"] += 1

    # ---- apertura y vigilancia -------------------------------------------------------------------
    def open(self) -> None:
        """Abre el endpoint. HapticError con el remedio si no se puede."""
        if NUMPY_ERROR is not None:
            raise HapticError(f"falta numpy ({NUMPY_ERROR}): python -m pip install numpy sounddevice")
        if SD_ERROR is not None:
            raise HapticError(f"falta sounddevice ({SD_ERROR}): python -m pip install numpy sounddevice")
        with self.open_lock:
            self._close_stream()
            cfg = self.device_cfg
            index, info = find_device(cfg.name)
            nch = int(info["max_output_channels"])
            if max(cfg.channels) >= nch:
                raise HapticError(f"channels = {cfg.channels[0] + 1},{cfg.channels[1] + 1} pero '{info['name']}' "
                                  f"tiene {nch} canales")
            fs = float(info["default_samplerate"])
            if fs != self.fs:
                self.fs = fs
                if self.profile is not None:
                    self.set_profile(self.profile)          # re-renderiza las voces a la nueva frecuencia
            self.nch, (self.ch_l, self.ch_r) = nch, cfg.channels
            self.clock, self.duck_until, self.voices = 0, 0, []
            for osc in self.osc:
                osc.env = osc.phase = 0.0
            attempts = [self.exclusive, False] if self.exclusive else [False]
            last: Exception | None = None
            for exclusive in attempts:
                try:
                    stream = sd.OutputStream(
                        device=index, samplerate=fs, channels=nch, dtype="float32", latency=cfg.latency,
                        callback=self._callback, finished_callback=self._finished,
                        extra_settings=sd.WasapiSettings(exclusive=exclusive))
                    stream.start()
                except Exception as error:  # noqa: BLE001 — PortAudioError y ValueError
                    last = error
                    if exclusive:
                        self.say(f"HAPT  modo exclusivo rechazado ({error}); pruebo compartido")
                    continue
                self.stream, self.exclusive = stream, exclusive
                self.device_name = info["name"]
                self.open_error = None
                self.counts["aperturas"] += 1
                self.say(f"HAPT  abierto [{index}] {info['name']}: {nch} canales a {fs:.0f} Hz, WASAPI "
                         f"{'exclusivo' if exclusive else 'compartido'}, latencia de salida "
                         f"{stream.latency * 1000:.1f} ms, hápticos en canales {self.ch_l + 1} (izq) y "
                         f"{self.ch_r + 1} (der)")
                if fs != 48000.0:
                    self.say(f"HAPT  AVISO: el endpoint está a {fs:.0f} Hz (lo esperado son 48000 Hz)")
                return
            raise HapticError(f"no pude abrir '{info['name']}': {last}")

    def _close_stream(self) -> None:
        stream, self.stream = self.stream, None
        if stream is not None:
            try:
                stream.abort()
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def start(self) -> None:
        """Abre (si puede) y arranca un vigilante que reabre tras desconexiones."""
        try:
            self.open()
        except HapticError as error:
            self.open_error = str(error)
            self.say(f"HAPT  NO DISPONIBLE: {str(error).rstrip('.')}. Reintento cada 3 s.")
        self.watchdog = threading.Thread(target=self._watch, name="haptics-watchdog", daemon=True)
        self.watchdog.start()

    def _watch(self) -> None:
        blocks = -1
        while not self.stop.wait(3.0):
            stream = self.stream
            # Vivo = activo y con callbacks nuevos (al quitar el USB, WASAPI puede dejar el stream
            # "activo" sin volver a llamar al callback).
            alive = stream is not None and stream.active and self.counts["bloques"] != blocks
            blocks = self.counts["bloques"]
            if alive:
                continue
            if stream is not None:
                self.say("HAPT  el stream se detuvo (¿mando desconectado?): reabro")
            try:
                if sd is not None:
                    with self.open_lock:
                        self._close_stream()
                        sd._terminate()      # refresca la lista de dispositivos de PortAudio
                        sd._initialize()
                self.open()
            except HapticError as error:
                if str(error) != self.open_error:
                    self.say(f"HAPT  sigue sin abrir: {error}")
                self.open_error = str(error)
            except Exception as error:  # noqa: BLE001
                self.open_error = repr(error)

    def reopen(self) -> None:
        try:
            self.open()
        except HapticError as error:
            self.open_error = str(error)
            self.say(f"HAPT  NO DISPONIBLE: {error}")

    def close(self) -> None:
        self.stop.set()
        self.silence()
        time.sleep(0.05)
        with self.open_lock:
            self._close_stream()

    # ---- estadísticas ----------------------------------------------------------------------------
    def drain_reports(self) -> list[dict]:
        items = []
        while self.reports:
            try:
                items.append(self.reports.popleft())
            except IndexError:
                break
        return items

    def latency_text(self) -> str:
        values = sorted(self.latencies)
        if not values:
            return "e→DAC n/d"
        p50 = values[len(values) // 2]
        p99 = values[min(len(values) - 1, int(len(values) * 0.99))]
        return f"e→DAC p50 {p50:.1f} ms p99 {p99:.1f} ms (n={len(values)})"

    def stats_text(self) -> str:
        stream = self.stream
        state = "abierto" if stream is not None and stream.active else "CERRADO"
        c = self.counts
        voices = " ".join(f"{name}={c[f'voz_{name}']}" for name in VOICE_NAMES)
        text = (f"HAPT  {state} | {voices} | {self.latency_text()} | underflows {c['underflow']} "
                f"limitador {c['limitador']}/{c['bloques']} bloques | errores {c['errores_callback']}")
        if self.last_error:
            text += f" (último: {self.last_error})"
        return text


# ---- herramientas de línea de comandos ------------------------------------------------------------
def cmd_list() -> int:
    try:
        chosen = find_device("auto")[0]
    except HapticError as error:
        chosen = None
        print(f"DualSense: {error}\n")
    print("Salidas de audio (índice, API, canales, Hz, nombre):")
    for index, info, api in device_list():
        mark = "  <== hápticos" if index == chosen else ""
        print(f"  [{index:2d}] {api:<22} {info['max_output_channels']:2d} ch {info['default_samplerate']:7.0f} Hz  "
              f"{info['name']}{mark}")
    return 0 if chosen is not None else 1


def play_and_wait(out: HapticOutput, buf, label: str = "") -> None:
    if label:
        print(label, flush=True)
    out.buffer(buf)
    time.sleep(len(buf) / out.fs + 0.05)


def cmd_test(out: HapticOutput, gain: float) -> int:
    fs = out.fs
    pulse = lambda pan, hz: render_voice((Layer("SINE", hz, 250, gain, pan, 0),), fs)  # noqa: E731
    pause = 0.45
    steps = (("IZQUIERDO (canal %d)" % (out.ch_l + 1), -1.0), ("DERECHO (canal %d)" % (out.ch_r + 1), 1.0),
             ("AMBOS", 0.0))
    for label, pan in steps:
        print(f"\n>> {label}: 3 pulsos de 60 Hz y 3 de 160 Hz")
        for hz in (60, 60, 60, 160, 160, 160):
            play_and_wait(out, pulse(pan, hz))
            time.sleep(pause)
    print("\nListo si: los 3 primeros grupos se sintieron en el lado anunciado y el ALTAVOZ del mando no sonó "
          "(los actuadores zumban un poco: eso es normal).")
    return 0


def cmd_sweep(out: HapticOutput, gain: float, freqs: list[float], ms: float) -> int:
    print(f"Barrido a ganancia {gain}: anota la frecuencia que se siente MÁS FUERTE y la más 'limpia'.")
    for hz in freqs:
        buf = render_voice((Layer("SINE", hz, ms, gain, 0.0, 0),), out.fs)
        play_and_wait(out, buf, f"  {hz:5.0f} Hz")
        time.sleep(0.5)
    return 0


def cmd_hit_demo(out: HapticOutput, profile: HapticProfile) -> int:
    if out.bank.get("hit") is None:
        print("la voz hit está en OFF en este perfil")
        return 1
    steps = (("IZQUIERDA (-90°)", -90.0), ("DERECHA (+90°)", 90.0), ("DELANTE (0°)", 0.0), ("DETRÁS (180°)", 180.0),
             ("diagonal delante-derecha (+45°)", 45.0), ("diagonal detrás-izquierda (-135°)", -135.0),
             ("vuelta completa cada 45°", None))
    for label, azimuth in steps:
        if azimuth is None:
            print(f"\n>> {label}: 0, 45, 90, 135, 180, -135, -90, -45")
            for az in (0, 45, 90, 135, 180, -135, -90, -45):
                for name, gains in hit_mix(profile, az, int(profile.hit_full_amt), lambda n: out.bank.get(n) is not None):
                    out.voice(name, gains=gains)
                time.sleep(0.55)
            continue
        mix = hit_mix(profile, azimuth, int(profile.hit_full_amt), lambda n: out.bank.get(n) is not None)
        text = " + ".join(f"{name} (izq {g[0]:.2f} der {g[1]:.2f})" for name, g in mix)
        print(f"\n>> {label}: {text}  (3 golpes)")
        for _ in range(3):
            for name, gains in mix:
                out.voice(name, gains=gains)
            time.sleep(0.7)
    print("\nListo si: izquierda y derecha se distinguen sin mirar; delante se siente agudo y seco, detrás grave "
          "y pesado, los dos centrados.")
    return 0


def cmd_rumble(out: HapticOutput, spec: str) -> int:
    try:
        large, small, ms = (int(x) for x in spec.split(","))
    except ValueError:
        print("--rumble espera 'grande,pequeño,ms' (p. ej. 76,255,180)", file=sys.stderr)
        return 2
    print(f"rumble ({large},{small}) durante {ms} ms")
    out.rumble(large, small)
    time.sleep(ms / 1000.0)
    out.rumble(0, 0)
    time.sleep(0.3)
    return 0


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="UNDS Día 4: hápticos del DualSense por WASAPI (herramientas).")
    parser.add_argument("--config", default=str(here / "unds_haptics.ini"), help="unds_haptics.ini")
    parser.add_argument("--game", default="default", choices=GAMES, help="perfil para --play/--rumble")
    parser.add_argument("--list", action="store_true", help="lista las salidas de audio")
    parser.add_argument("--test", action="store_true", help="pulsos izquierdo / derecho / ambos")
    parser.add_argument("--sweep", nargs="?", const="20,30,40,50,60,80,100,125,150,175,200,250,300,400",
                        metavar="HZ,HZ,...", help="barrido de frecuencias (por defecto 20-400 Hz)")
    parser.add_argument("--sweep-ms", type=float, default=900.0, help="duración de cada paso del barrido (900)")
    parser.add_argument("--gain", type=float, default=0.5, help="ganancia de --test/--sweep (0.5)")
    parser.add_argument("--play", choices=VOICE_NAMES, help="reproduce una voz del perfil")
    parser.add_argument("--azimuth", type=float, help="con --play hit: ángulo de la fuente (0 delante, + derecha)")
    parser.add_argument("--amt", type=int, help="con --play hit: daño en milésimas de vida (rifle ~90)")
    parser.add_argument("--hit-demo", action="store_true", help="golpes desde izquierda, derecha, delante, detrás y vuelta de 360°")
    parser.add_argument("--rumble", metavar="L,S,MS", help="traduce un rumble de prueba")
    parser.add_argument("--device", help="subcadena del nombre (sustituye a [device] name)")
    parser.add_argument("--exclusive", action="store_true", help="WASAPI exclusivo")
    args = parser.parse_args(argv)
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    if NUMPY_ERROR or SD_ERROR:
        print(f"falta una dependencia ({NUMPY_ERROR or SD_ERROR}).\n  python -m pip install --upgrade numpy sounddevice",
              file=sys.stderr)
        return 2
    if args.list:
        return cmd_list()
    config = HapticConfig(Path(args.config))
    try:
        config.load()
    except ValueError as error:
        print(f"error en {args.config}: {error}", file=sys.stderr)
        return 2
    device = config.device
    if args.device or args.exclusive:
        device = DeviceConfig(name=args.device or device.name, exclusive=args.exclusive or device.exclusive,
                              latency=device.latency, channels=device.channels, master=device.master,
                              align_s=device.align_s)
    out = HapticOutput(device)
    profile = config.profile(args.game)
    out.set_profile(profile)
    try:
        out.open()
    except HapticError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        if args.test:
            return cmd_test(out, args.gain)
        if args.sweep:
            freqs = [float(x) for x in args.sweep.split(",") if x.strip()]
            return cmd_sweep(out, args.gain, freqs, args.sweep_ms)
        if args.play:
            buf = out.bank.get(args.play)
            if buf is None:
                print(f"la voz {args.play} está en OFF en el perfil [{args.game}]")
                return 1
            if args.play == "hit":
                mix = hit_mix(profile, args.azimuth, args.amt, lambda n: out.bank.get(n) is not None)
                print("hit: " + " + ".join(f"{n} (izq {g[0]:.2f} der {g[1]:.2f})" for n, g in mix))
                for name, gains in mix:
                    out.voice(name, gains=gains)
            else:
                print(f"{args.play}: {len(buf) / out.fs * 1000:.0f} ms")
                out.voice(args.play)
            time.sleep(len(buf) / out.fs + 0.2)
            return 0
        if args.hit_demo:
            return cmd_hit_demo(out, profile)
        if args.rumble:
            return cmd_rumble(out, args.rumble)
        parser.print_help()
        return 0
    except KeyboardInterrupt:
        return 130
    finally:
        print(out.stats_text())
        out.close()


if __name__ == "__main__":
    sys.exit(main())
