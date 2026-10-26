#!/usr/bin/env python3
"""UNDS — gatillos adaptativos y lightbar del DualSense (Día 3; HID directo desde el Día 4; por el port
desde el Día 8).

Backend ([output] backend en el .ini, o --backend): port (por defecto, unds_port.py: el companion manda
cada cambio al port por el canal fx y el port lo escribe con SDL; sin hidapi, USB o Bluetooth; requiere el
parche 0004) | hid (unds_hid.py: el companion escribe el reporte USB del mando) | dsx (Mod System UDP de
DSX). Con cualquiera, DSX debe estar CERRADO: abierto apaga los hápticos.

Escucha el bus de eventos del port (igual que unds_listen.py: misma salida, mismas grabaciones
JSONL y estadísticas) y traduce el estado a efectos de DSX:

  - L2: efecto fijo de "peso" al apuntar (l2).
  - R2: r2_idle sin apuntar, r2_aim con L2 pulsado.
  - Disparo: flanco de subida del rumble con la firma de disparo (motor pequeño >= fire_s_min y
    grande <= fire_l_max, con R2 pulsado en los últimos fire_r2_window_ms) -> `kick` durante
    kick_ms en R2 y vuelta al efecto anterior. Es el disparo real (Día 2), no la intención.
  - Menú/pausa (lightbar del juego == menu_rgb): gatillos menu_l2 / menu_r2.
  - Lightbar: la del juego, enviada a DSX (el port debe correr con UNDS_LED=0, parche 0003).
  - Fin del juego (bye o silencio > --timeout) y Ctrl+C: ResetToUserSettings en DSX.
  - Estado del juego (Día 5, parche 0004, U1 1.00): con eventos gs/shot/dmg del port, la vida REAL
    tiñe la lightbar, cada "shot" es un disparo exacto (sin firmas de rumble ni temporizador de
    ráfaga), "dmg" da el destello de daño y el cargador vacío pone empty_r2. Sin esos eventos
    (U2/U3, otra versión, UNDS_STATE=0) todo sigue como en el Día 4.
  - Kicks como secuencia (Día 6): kick / auto_kick = "EFECTO @ms > EFECTO @ms ...", una fase tras
    otra en cada disparo (p. ej. empuje del gatillo y luego vibración). Un solo efecto sin @ dura
    kick_ms / auto_kick_ms, como antes. Con backend = port o hid también hay MACHINE (efecto 0x27).
  - Perfiles por arma (Día 7): con estado real cada arma tiene una CLASE (pistola, revolver,
    subfusil, automatica, escopeta, lanzagranadas, torreta, granada) que cambia l2, r2_idle, r2_aim,
    kick, auto_kick y empty_r2. Orden: [default] < [u1] < clase (código) < [clase X] < [arma 0x...].
    Un id sin sección se clasifica solo (munición infinita = torreta, cargador 1 = lanzagranadas,
    disparos seguidos = automatica, si no pistola). Sin arma (w = 0x0) un rato: unarmed_l2/r2.

La configuración (unds_triggers.ini) se recarga sola al guardarla.

    python unds_triggers.py                          # 127.0.0.1:47111, graba en .\\recordings\\
    python unds_triggers.py --calibrate              # R2 = r2_aim fijo; mide dónde rompe el gatillo
    python unds_triggers.py --dry-run                # no envía nada a DSX, solo imprime decisiones

Solo biblioteca estándar (Python 3.10+). Requiere unds_listen.py y unds_dsx.py en la misma carpeta.
"""
from __future__ import annotations

import argparse
import configparser
import os
import signal
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from unds_dsx import HID_ONLY_MODES, LEFT, RIGHT, DSXClient, Effect, parse_effect, pick_controller  # noqa: E402
from unds_listen import Emitter, Listener  # noqa: E402

BTN_L2, BTN_R2 = 0x100, 0x200
DEFAULTS = {
    "l2": "FEEDBACK 2 1",
    "r2_idle": "WEAPON 3 4 3",
    "r2_aim": "WEAPON 3 4 6",
    "kick": "VIBRATION 1 8 40",
    "kick_ms": "70",
    "menu_l2": "OFF",
    "menu_r2": "OFF",
    "menu_rgb": "0,0,255",
    "menu_debounce_ms": "150",
    "death": "hold",
    "death_rgb": "64,0,0",
    "death_hold_ms": "6000",
    "death_fade_ms": "1500",
    "death_l2": "OFF",
    "death_r2": "OFF",
    "fire_s_min": "200",
    "fire_l_min": "76",
    "fire_l_max": "120",
    "auto_l": "114",
    "auto_period_ms": "106",
    "auto_kick": "VIBRATION 1 8 40",
    "auto_kick_ms": "50",
    "damage": "on",
    "damage_rgb": "255,40,0",
    "damage_step": "0.25",
    "damage_hold_ms": "2500",
    "damage_flash_ms": "180",
    "damage_recover_ms": "4000",
    "context": "combat",
    "combat_s": "40",
    "aim_arm_ms": "250",
    "calm_l2": "OFF",
    "calm_r2": "OFF",
    "fire_r2_window_ms": "800",
    "led": "game",
    "led_brightness": "255",
    "led_gain": "4.0",
    "state": "auto",
    "fire_source": "auto",
    "auto_gap_ms": "200",
    "empty_r2": "OFF",
    "health_curve": "1.0",
    "health_led": "game",
    "cutscene": "state",
    "cutscene_l2": "OFF",
    "cutscene_r2": "OFF",
    "unarmed_l2": "OFF",
    "unarmed_r2": "OFF",
    "unarmed_ms": "1000",
    # Día 8b: armas montadas en vehículos (U1: la torreta del jeep al escapar de Eddy). No pasan por
    # WeaponController::Fire (sin evento shot, w = 0x0): se reconocen por el rumble que el juego pone
    # entre 3 y 6 ms después de pulsar (R2: 114/255 sostenido mientras se dispara; L2: pulso de 255).
    "mounted": "on",
    "mounted_window_ms": "30",
    "mounted_hold_ms": "30000",
    "mounted_period_ms": "120",
    "mounted_r2_class": "torreta",           # R2 con rumble sostenido (ráfaga)
    "mounted_r2_single_class": "lanzagranadas",  # R2 con pulso de 255 (disparo único; moto acuática)
    "mounted_r2_light_class": "pistola",     # R2 con firma de pistola, no sostenida (moto acuática, 2.ª parte)
    "mounted_l2_class": "lanzagranadas",     # L2 con pulso de 255 (granadas del jeep)
    "mounted_l2": "FEEDBACK 2 4",
}
# Día 7: lo que una clase o un arma puede cambiar, y los valores de cada clase (sobre el perfil del juego).
# pistola y automatica = los efectos aprobados del Día 6 (no cambian nada). Todos los WEAPON terminan en la
# zona 4: el umbral de pulsación del port (UNDS_TRIGGER=30,95) es uno para todas las armas y está calibrado
# justo después del tope de WEAPON 3 4; un tope más hondo dispararía antes de romperlo. Las clases se
# distinguen por el inicio (tope más largo desde la zona 2) y la fuerza.
WEAPON_KEYS = ("l2", "r2_idle", "r2_aim", "kick", "kick_ms", "auto_kick", "auto_kick_ms", "empty_r2")
CLASS_DEFAULTS: dict[str, dict[str, str]] = {
    "pistola": {"automatica": "no"},
    "revolver": {"automatica": "no", "r2_idle": "WEAPON 3 4 7", "r2_aim": "WEAPON 3 4 8",
                 "kick": "FEEDBACK 1 8 @45 > VIBRATION 1 6 30 @40"},
    "subfusil": {"automatica": "si", "r2_idle": "WEAPON 3 4 4", "r2_aim": "WEAPON 3 4 6",
                 "auto_kick": "FEEDBACK 1 6 @40 > VIBRATION 1 6 40 @20"},
    "automatica": {"automatica": "si"},
    "escopeta": {"automatica": "no", "r2_idle": "WEAPON 2 4 6", "r2_aim": "WEAPON 2 4 8",
                 "kick": "FEEDBACK 1 8 @70 > VIBRATION 1 8 25 @60"},
    "lanzagranadas": {"automatica": "no", "r2_idle": "WEAPON 2 4 8", "r2_aim": "WEAPON 2 4 8",
                      "kick": "FEEDBACK 1 8 @110 > VIBRATION 1 8 15 @140"},
    "torreta": {"automatica": "si", "l2": "OFF", "r2_idle": "FEEDBACK 3 4", "r2_aim": "FEEDBACK 3 4",
                "auto_kick": "FEEDBACK 1 8 @50 > VIBRATION 1 8 25 @35"},
    "granada": {"automatica": "no", "r2_idle": "OFF", "r2_aim": "OFF", "kick": "OFF @1"},
}
CLASSES = tuple(CLASS_DEFAULTS)


@dataclass(frozen=True)
class WeaponInfo:
    name: str                    # nombre que pone el usuario en [arma 0x...] (o "")
    cls: str | None              # clase fija, o None = clasificar sola
    automatic: bool | None       # None = aprenderlo por la cadencia
DSX_DEFAULTS = {"port": "auto", "controller": "auto", "reassert_s": "2.0", "status_s": "5.0", "min_interval_ms": "4"}
OUTPUT_DEFAULTS = {"backend": "port"}
BACKENDS = ("port", "hid", "dsx")
ALERT_BLINK_S = 0.25            # Día 9: medio periodo del parpadeo rojo de aviso del lanzador


@dataclass(frozen=True)
class Profile:
    l2: Effect
    r2_idle: Effect
    r2_aim: Effect
    kick: tuple                  # ((Effect, segundos), ...) fases del golpe en R2 por disparo
    kick_s: float                # duración total
    menu_l2: Effect
    menu_r2: Effect
    menu_rgb: tuple[int, int, int]
    menu_debounce_s: float
    death_hold: bool
    death_rgb: tuple[int, int, int]
    death_hold_s: float
    death_fade_s: float
    death_l2: Effect
    death_r2: Effect
    fire_s_min: int
    fire_l_min: int
    fire_l_max: int
    fire_r2_window_s: float
    auto_l: frozenset
    auto_period_s: float
    auto_kick: tuple
    auto_kick_s: float
    damage_on: bool
    damage_rgb: tuple[int, int, int]
    damage_step: float
    damage_flash_s: float
    damage_hold_s: float
    damage_recover_s: float
    context_always: bool
    combat_s: float
    aim_arm_s: float
    calm_l2: Effect
    calm_r2: Effect
    led_game: bool
    led_brightness: int
    led_gain: float
    state_on: bool
    fire_source: str
    auto_gap_s: float
    empty_r2: Effect
    health_curve: float
    health_led: str
    cutscene_on: bool            # Día 6: jugador sin actualizar (ups = 0) con la luz normal = cinemática
    cutscene_l2: Effect
    cutscene_r2: Effect
    unarmed_l2: Effect           # Día 7: sin arma (w = 0x0) durante unarmed_s
    unarmed_r2: Effect
    unarmed_s: float
    mounted_on: bool             # Día 8b: arma montada (sin arma en la mano, firma de rumble al pulsar)
    mounted_window_s: float
    mounted_hold_s: float
    mounted_period_s: float
    mounted_r2_class: str
    mounted_r2_single_class: str
    mounted_r2_light_class: str
    mounted_l2_class: str
    mounted_l2: Effect


def parse_kick(text: str, default_ms: int, what: str) -> tuple[tuple[Effect, float], ...]:
    """'VIBRATION 1 8 40' (dura default_ms) o 'FEEDBACK 1 8 @55 > VIBRATION 1 8 40 @25' (fases)."""
    phases = []
    chunks = [c.strip() for c in text.split(">")]
    for chunk in chunks:
        if not chunk:
            raise ValueError(f"{what} = {text!r}: fase vacía")
        effect_text, _, ms_text = chunk.partition("@")
        if ms_text.strip():
            ms = int(ms_text)
        elif len(chunks) == 1:
            ms = default_ms
        else:
            raise ValueError(f"{what}: con varias fases cada una lleva su duración, p. ej. 'FEEDBACK 1 8 @55'")
        if not 1 <= ms <= 2000:
            raise ValueError(f"{what}: duración {ms} ms fuera de 1-2000")
        phases.append((parse_effect(effect_text), ms / 1000.0))
    return tuple(phases)


def profile_effects(p: "Profile") -> list[Effect]:
    return [p.l2, p.r2_idle, p.r2_aim, *(e for e, _ in p.kick), p.menu_l2, p.menu_r2, p.death_l2, p.death_r2,
            *(e for e, _ in p.auto_kick), p.calm_l2, p.calm_r2, p.empty_r2, p.mounted_l2]


def parse_rgb(text: str) -> tuple[int, int, int]:
    parts = [int(p) for p in text.replace(" ", "").split(",")]
    if len(parts) != 3 or not all(0 <= p <= 255 for p in parts):
        raise ValueError(f"rgb {text!r}: se esperan tres enteros 0-255 separados por comas")
    return parts[0], parts[1], parts[2]


def parse_choice(text: str, options: tuple[str, ...], what: str) -> str:
    value = text.strip().lower()
    if value not in options:
        raise ValueError(f"{what} = {text!r}: {' | '.join(options)}")
    return value


def lerp_rgb(a: tuple[int, int, int], b: tuple[int, int, int], k: float) -> tuple[int, int, int]:
    k = max(0.0, min(1.0, k))
    return tuple(round(x + (y - x) * k) for x, y in zip(a, b))  # type: ignore[return-value]


def scale_rgb(rgb: tuple[int, int, int], gain: float) -> tuple[int, int, int]:
    """Multiplica el color sin cambiar el tono: si un canal saturaría, se limita la ganancia."""
    top = max(rgb)
    if top == 0 or gain <= 0:
        return (0, 0, 0) if gain <= 0 else rgb
    k = min(gain, 255.0 / top)
    return tuple(min(255, round(c * k)) for c in rgb)  # type: ignore[return-value]


def build_profile(values: dict[str, str]) -> Profile:
    """Valores del .ini ya mezclados -> Profile. KeyError/ValueError si algo no es válido."""
    return Profile(
            l2=parse_effect(values["l2"]), r2_idle=parse_effect(values["r2_idle"]),
            r2_aim=parse_effect(values["r2_aim"]),
            kick=(kick := parse_kick(values["kick"], int(values["kick_ms"]), "kick")),
            kick_s=sum(d for _, d in kick), menu_l2=parse_effect(values["menu_l2"]),
            menu_r2=parse_effect(values["menu_r2"]), menu_rgb=parse_rgb(values["menu_rgb"]),
            menu_debounce_s=int(values["menu_debounce_ms"]) / 1000.0,
            death_hold=values["death"].strip().lower() == "hold",
            death_rgb=parse_rgb(values["death_rgb"]),
            death_hold_s=int(values["death_hold_ms"]) / 1000.0,
            death_fade_s=max(0.001, int(values["death_fade_ms"]) / 1000.0),
            death_l2=parse_effect(values["death_l2"]), death_r2=parse_effect(values["death_r2"]),
            fire_s_min=int(values["fire_s_min"]), fire_l_min=int(values["fire_l_min"]),
            fire_l_max=int(values["fire_l_max"]),
            fire_r2_window_s=int(values["fire_r2_window_ms"]) / 1000.0,
            auto_l=frozenset(int(v) for v in values["auto_l"].replace(" ", "").split(",") if v),
            auto_period_s=max(0.03, int(values["auto_period_ms"]) / 1000.0),
            auto_kick=(auto_kick := parse_kick(values["auto_kick"], int(values["auto_kick_ms"]), "auto_kick")),
            auto_kick_s=sum(d for _, d in auto_kick),
            damage_on=values["damage"].strip().lower() in ("on", "yes", "1", "true"),
            damage_rgb=parse_rgb(values["damage_rgb"]),
            damage_step=max(0.0, min(1.0, float(values["damage_step"]))),
            damage_flash_s=int(values["damage_flash_ms"]) / 1000.0,
            damage_hold_s=max(0.0, int(values["damage_hold_ms"]) / 1000.0),
            damage_recover_s=max(0.1, int(values["damage_recover_ms"]) / 1000.0),
            context_always=values["context"].strip().lower() == "always",
            combat_s=float(values["combat_s"]),
            aim_arm_s=int(values["aim_arm_ms"]) / 1000.0,
            calm_l2=parse_effect(values["calm_l2"]), calm_r2=parse_effect(values["calm_r2"]),
            led_game=values["led"].strip().lower() == "game",
            led_brightness=max(0, min(255, int(values["led_brightness"]))),
            led_gain=max(0.0, float(values["led_gain"])),
            state_on=parse_choice(values["state"], ("auto", "off"), "state") == "auto",
            fire_source=parse_choice(values["fire_source"], ("auto", "rumble", "state"), "fire_source"),
            auto_gap_s=max(0.03, int(values["auto_gap_ms"]) / 1000.0),
            empty_r2=parse_effect(values["empty_r2"]),
            health_curve=max(0.1, min(5.0, float(values["health_curve"]))),
            health_led=parse_choice(values["health_led"], ("game", "state"), "health_led"),
            cutscene_on=parse_choice(values["cutscene"], ("state", "off"), "cutscene") == "state",
            cutscene_l2=parse_effect(values["cutscene_l2"]),
            cutscene_r2=parse_effect(values["cutscene_r2"]),
            unarmed_l2=parse_effect(values["unarmed_l2"]), unarmed_r2=parse_effect(values["unarmed_r2"]),
            unarmed_s=max(0.0, int(values["unarmed_ms"]) / 1000.0),
            mounted_on=parse_choice(values["mounted"], ("on", "off"), "mounted") == "on",
            mounted_window_s=max(0.005, min(0.2, int(values["mounted_window_ms"]) / 1000.0)),
            mounted_hold_s=max(1.0, int(values["mounted_hold_ms"]) / 1000.0),
            mounted_period_s=max(0.03, int(values["mounted_period_ms"]) / 1000.0),
            mounted_r2_class=parse_choice(values["mounted_r2_class"], CLASSES, "mounted_r2_class"),
            mounted_r2_single_class=parse_choice(values["mounted_r2_single_class"], CLASSES,
                                                 "mounted_r2_single_class"),
            mounted_r2_light_class=parse_choice(values["mounted_r2_light_class"], CLASSES,
                                                "mounted_r2_light_class"),
            mounted_l2_class=parse_choice(values["mounted_l2_class"], CLASSES, "mounted_l2_class"),
            mounted_l2=parse_effect(values["mounted_l2"]))


def parse_weapon_id(text: str) -> str:
    """'0x58D20069' / '58d20069' -> '0x58d20069' (como llega en los eventos del port)."""
    value = int(text.strip(), 16)
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"id de arma {text!r} fuera de 32 bits")
    return f"0x{value:x}"


def parse_auto(text: str, what: str) -> bool | None:
    value = text.strip().lower()
    if value in ("auto", ""):
        return None
    if value in ("si", "sí", "yes", "on", "1", "true"):
        return True
    if value in ("no", "off", "0", "false"):
        return False
    raise ValueError(f"{what}: automatica = {text!r}: si | no | auto")


class Config:
    def __init__(self, path: Path, backend: str | None = None):
        self.path = path
        self.backend_override = backend        # --backend (gana al .ini)
        self.mtime = 0.0
        self.parser = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
        self.profiles: dict[str, Profile] = {}
        self.dsx: dict[str, str] = dict(DSX_DEFAULTS)
        self.output: dict[str, str] = dict(OUTPUT_DEFAULTS)

    def load(self) -> None:
        """Lee el .ini. ValueError con un mensaje claro si algo no es válido (no cambia nada)."""
        parser = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
        if self.path.exists():
            parser.read(self.path, encoding="utf-8")
        default = dict(DEFAULTS)
        if parser.has_section("default"):
            default.update(parser["default"])
        profiles, game_values = {}, {}
        for game in ("default", "u1", "u2", "u3", "other"):
            values = dict(default)
            if game != "default" and parser.has_section(game):
                values.update(parser[game])
            game_values[game] = values
            try:
                profiles[game] = build_profile(values)
            except (KeyError, ValueError) as error:
                raise ValueError(f"[{game}] {error}") from None
        weapons, class_values = self.load_weapons(parser)
        dsx = dict(DSX_DEFAULTS)
        if parser.has_section("dsx"):
            dsx.update(parser["dsx"])
        output = dict(OUTPUT_DEFAULTS)
        if parser.has_section("output"):
            output.update(parser["output"])
        output["backend"] = (self.backend_override or output["backend"]).strip().lower()
        if output["backend"] not in BACKENDS:
            raise ValueError(f"[output] backend = {output['backend']!r}: {' | '.join(BACKENDS)}")
        if output["backend"] == "dsx":
            for game, prof in profiles.items():
                hid_only = [str(e) for e in profile_effects(prof) if e.mode in HID_ONLY_MODES]
                if hid_only:
                    raise ValueError(f"[{game}] {hid_only[0]}: solo existe con [output] backend = port o hid")
        self.profiles, self.dsx, self.output, self.parser = profiles, dsx, output, parser
        self.game_values, self.weapons, self.class_values = game_values, weapons, class_values
        self.weapon_profiles: dict[tuple[str, str, str], Profile] = {}
        # Validar ya todas las clases y armas declaradas (error claro al guardar, no en mitad del juego)
        for cls in CLASSES:
            self.weapon_profile("default", "", cls)
        for wid, info in weapons.items():
            self.weapon_profile("default", wid, info.cls or "pistola")
        if output["backend"] == "dsx":
            for key, prof in self.weapon_profiles.items():
                hid_only = [str(e) for e in profile_effects(prof) if e.mode in HID_ONLY_MODES]
                if hid_only:
                    raise ValueError(f"[{key[2] if not key[1] else 'arma ' + key[1]}] {hid_only[0]}: solo existe "
                                     f"con [output] backend = port o hid")
        self.mtime = self.path.stat().st_mtime if self.path.exists() else 0.0

    @staticmethod
    def load_weapons(parser: configparser.ConfigParser) -> tuple[dict[str, WeaponInfo], dict[str, dict[str, str]]]:
        """[clase X] y [arma 0x...]: solo WEAPON_KEYS (+ automatica; + nombre y clase en las armas)."""
        weapons: dict[str, WeaponInfo] = {}
        classes: dict[str, dict[str, str]] = {c: dict(v) for c, v in CLASS_DEFAULTS.items()}
        for section in parser.sections():
            kind, _, ident = section.strip().partition(" ")
            kind = kind.lower()
            if kind not in ("clase", "arma"):
                continue
            items = dict(parser[section])
            allowed = set(WEAPON_KEYS) | {"automatica"} | ({"nombre", "clase"} if kind == "arma" else set())
            unknown = sorted(set(items) - allowed)
            if unknown:
                raise ValueError(f"[{section}] clave(s) {', '.join(unknown)} no válidas aquí "
                                 f"(se admiten: {', '.join(sorted(allowed))})")
            if kind == "clase":
                cls = ident.strip().lower()
                if cls not in classes:
                    raise ValueError(f"[{section}]: clase desconocida; clases: {', '.join(CLASSES)}")
                classes[cls].update(items)
                parse_auto(classes[cls].get("automatica", "auto"), f"[{section}]")
                continue
            try:
                wid = parse_weapon_id(ident)
            except ValueError:
                raise ValueError(f"[{section}]: se espera [arma 0x58d20069] (id del evento shot/gs)") from None
            cls = items.pop("clase", "").strip().lower() or None
            if cls is not None and cls not in CLASS_DEFAULTS:
                raise ValueError(f"[{section}] clase = {cls!r}: {' | '.join(CLASSES)}")
            weapons[wid] = WeaponInfo(name=items.pop("nombre", "").strip(), cls=cls,
                                      automatic=parse_auto(items.pop("automatica", "auto"), f"[{section}]"))
            classes.setdefault(f"arma {wid}", {}).update(items)
        return weapons, classes

    def class_automatic(self, cls: str) -> bool | None:
        return parse_auto(self.class_values[cls].get("automatica", "auto"), cls)

    def weapon_profile(self, game: str, wid: str, cls: str) -> Profile:
        """Perfil del juego con lo de la clase y lo del arma encima (cacheado hasta recargar)."""
        game = game if game in self.game_values else "default"
        key = (game, wid, cls)
        prof = self.weapon_profiles.get(key)
        if prof is None:
            values = dict(self.game_values[game])
            values.update({k: v for k, v in self.class_values[cls].items() if k in WEAPON_KEYS})
            values.update({k: v for k, v in self.class_values.get(f"arma {wid}", {}).items() if k in WEAPON_KEYS})
            try:
                prof = build_profile(values)
            except (KeyError, ValueError) as error:
                where = f"arma {wid}" if wid in self.weapons else f"clase {cls}"
                raise ValueError(f"[{where}] {error}") from None
            self.weapon_profiles[key] = prof
        return prof

    def changed(self) -> bool:
        try:
            return self.path.stat().st_mtime != self.mtime
        except OSError:
            return False

    def profile(self, game: str) -> Profile:
        return self.profiles.get(game) or self.profiles["default"]


class Engine:
    """Estado del juego -> efectos deseados. Un hilo propio envía a DSX solo lo que cambia."""

    def __init__(self, config: Config, args: argparse.Namespace, say):
        self.config, self.args, self.say = config, args, say
        self.backend = config.output["backend"]
        self.label = {"hid": "HID", "port": "PORT"}.get(self.backend, "DSX")
        if args.dry_run:
            self.client = None
        elif self.backend == "port":
            from unds_port import PortClient
            self.client = PortClient(say)
        elif self.backend == "hid":
            from unds_hid import HidClient
            try:
                self.client = HidClient(say)
            except RuntimeError as error:
                raise ValueError(str(error)) from None
        else:
            port = None if config.dsx["port"].strip().lower() == "auto" else int(config.dsx["port"])
            self.client = DSXClient(port=port)
        self.cond = threading.Condition()
        self.stop = False
        self.stop_event = threading.Event()   # el hilo de estado no comparte la condición del emisor
        # estado del juego (bajo cond)
        self.attached: str | None = None      # u1/u2/u3 del emisor activo
        self.buttons = 0
        self.r2_down_at = -1e9
        self.led_rgb: tuple[int, int, int] | None = None
        self.led_prev: tuple[int, int, int] | None = None   # color anterior (para el parpadeo azul)
        self.led_since = 0.0
        self.death_phase: str | None = None   # None | "hold" | "fade"
        self.death_t = 0.0
        self.fade_t = 0.0
        self.rumble = (0, 0)
        self.kick_until = 0.0
        self.kick_t = 0.0                      # inicio del kick en curso
        self.kick_seq: tuple = ()              # sus fases ((Effect, s), ...)
        self.calibrating = args.calibrate
        self.l2_down_at = -1e9
        self.r2_up_at = -1e9
        self.last_combat = -1e9                # último disparo propio, daño recibido o apuntado largo
        self.armed_shown: bool | None = None   # para avisar de cambios combate/calma
        self.auto_active = False               # ráfaga de arma automática en curso
        self.auto_next = 0.0
        self.auto_shots = 0
        self.damage_level = 0.0                # 0-1: tinte de daño de la lightbar (decae)
        self.damage_t = 0.0
        self.last_damage_rx = -1e9
        self.alert_t = 0.0                     # Día 9: aviso del lanzador (lightbar roja parpadeante)
        self.alert_until = 0.0
        # estado real del juego (Día 5; eventos gs/shot/dmg del parche 0004)
        self.reset_state()
        # lo último enviado
        self.sent: dict[str, object] = {}
        self.sent_at = 0.0
        self.controller: int | None = None
        self.status_at = 0.0
        self.no_reply_warned = False
        self.counts: Counter = Counter()
        self.recorder = None                   # función write(dict) del listener
        self.on_event = None                   # callable(nombre): "death" | "respawn" (unds.py -> hápticos)
        self.thread = threading.Thread(target=self.run, name="dsx-tx", daemon=True)

    def alert(self, seconds: float) -> None:
        """Día 9: la lightbar parpadea en rojo `seconds` segundos por encima de todo lo demás (aviso del
        lanzador: arranque defectuoso, pérdida de datos o aserción del port, visible a pantalla completa)."""
        with self.cond:
            now = time.perf_counter()
            self.alert_t, self.alert_until = now, now + max(0.0, seconds)
            self.cond.notify_all()

    def reset_state(self) -> None:
        self.gs_at = -1e9                      # último gs con vida válida
        self.hp = 1.0
        self.injured = False
        self.weapon = "0x0"
        self.clip = self.cap = self.reserve = -1
        self.player_active: bool | None = None # refresco gs: ups > 0
        self.last_shot_t = -1e9
        self.last_shot_w = ""
        self.auto_weapons: set[str] = set()
        self.weapon_caps: dict[str, int] = {}  # tamaño de cargador visto por arma (clasificación)
        self.weapon_since = 0.0                # desde cuándo está el arma actual (sin arma: unarmed_ms)
        self.weapons_seen: dict[str, str] = {} # id -> clase (resumen)
        self.hp_zero_at: float | None = None
        self.led_healthy: tuple[int, int, int] | None = None
        # Día 8b: arma montada
        self.mounted_until = -1e9              # hasta cuándo sigue el modo (se renueva con cada disparo)
        self.mounted_shown: tuple | None = None   # (papel de R2, papel de L2) anunciado
        self.mount_r2: str | None = None       # papel de R2: "auto" (ráfaga) | "single" (disparo único)
        self.mount_r2_cls = ""                 # clase del disparo único de R2 (lanzagranadas | pistola)
        self.mount_r2_press = -1.0             # r2_down_at de la pulsación ya clasificada (una vez por pulsación)
        self.mount_l2: str | None = None       # papel de L2: "single" (granada) | None (apuntar)
        self.mount_auto = False                # ráfaga de la torreta (R2) en curso
        self.mount_next = 0.0
        self.mount_shots = 0
        self.lkick_until = 0.0                 # golpe en L2 (granada)
        self.lkick_t = 0.0
        self.lkick_seq: tuple = ()

    def state_live(self, now: float, p: Profile) -> bool:
        """Hay estado real: el port publica gs con vida válida (hooks de esta versión del juego)."""
        return p.state_on and now - self.gs_at < 2.0

    def fire_from_state(self, now: float, p: Profile) -> bool:
        if p.fire_source == "rumble":
            return False
        if p.fire_source == "state":
            return True
        return self.state_live(now, p)

    # ---- entradas (hilo del listener) -----------------------------------------------------------
    def attach(self, game: str) -> None:
        with self.cond:
            self.attached, self.buttons, self.kick_until, self.rumble = game, 0, 0.0, (0, 0)
            self.auto_active, self.damage_level, self.last_combat, self.armed_shown = False, 0.0, -1e9, None
            self.reset_state()
            self.sent.clear()                  # reenviar todo al juego nuevo
            self.cond.notify_all()

    def detach(self, why: str) -> None:
        with self.cond:
            if self.attached is None:
                return
            self.attached = None
            self.cond.notify_all()
        self.say(f"{self.label}  fin de juego ({why}): gatillos libres"
                 f"{' y perfil de DSX' if self.backend == 'dsx' else ''}")

    def pad(self, buttons: int) -> None:
        now = time.perf_counter()
        with self.cond:
            if (buttons & BTN_R2) and not (self.buttons & BTN_R2):
                self.r2_down_at = now
            if not (buttons & BTN_R2) and (self.buttons & BTN_R2):
                self.r2_up_at = now
            if (buttons & BTN_L2) and not (self.buttons & BTN_L2):
                self.l2_down_at = now
            if (buttons ^ self.buttons) & (BTN_L2 | BTN_R2):
                self.cond.notify_all()
            self.buttons = buttons

    def led(self, rgb: tuple[int, int, int]) -> None:
        """Lightbar del juego. En U1 es un indicador de VIDA [VERIFICADO 2026-10-23, grabación 142809]:
        (32,32,0) sano -> (64,0,0) vida crítica, degradado al recibir daño y al regenerarse. (64,0,0)
        también sale al morir; por eso sin estado real se toma como muerte (y falla con vida crítica:
        dos falsas muertes en esa grabación). Un azul de ~35 ms ~4 s después = reaparición; azul
        sostenido = pausa/menú/carga."""
        now = time.perf_counter()
        with self.cond:
            if rgb == self.led_rgb:
                return
            self.led_prev, self.led_rgb, self.led_since = self.led_rgb, rgb, now
            if self.attached is not None:
                p = self.config.profile(self.attached)
                live = self.state_live(now, p)
                if p.death_hold and rgb == p.death_rgb and self.death_phase != "hold" and not live:
                    self.start_death(now, "LED   muerte: rojo fijo y gatillos libres hasta reaparecer")
                elif self.death_phase == "hold" and rgb == p.menu_rgb:
                    self.end_death(now, f"LED   reaparición tras {now - self.death_t:.1f} s: degradado al color del juego")
                if not live or self.hp >= 0.999:
                    if rgb != p.menu_rgb and rgb != p.death_rgb and self.death_phase is None:
                        self.led_healthy = rgb                  # color del juego con la vida llena
            self.cond.notify_all()

    def start_death(self, now: float, text: str) -> None:
        """Bajo cond."""
        self.death_phase, self.death_t = "hold", now
        self.counts["muertes"] += 1
        self.say(text)
        self.auto_active, self.damage_level = False, 0.0
        if self.on_event:
            self.on_event("death")

    def end_death(self, now: float, text: str) -> None:
        """Bajo cond: reaparición."""
        self.death_phase, self.fade_t = "fade", now
        self.say(text)
        if self.on_event:
            self.on_event("respawn")

    def rumble_event(self, l: int, s: int) -> str | None:
        """Clasifica cada rumble del juego. Devuelve 'fire' (disparo propio de arma semiautomática),
        'fire_auto' (inicio de ráfaga de arma automática), 'hit' (daño recibido), 'other' o None.

        Firmas en U1 (grabación 2026-10-22 19:06):
          disparo de pistola    (76,255) ~115 ms -> (76,0) -> (0,0); un solo valor sostenido
          AK-47 (automática)    (114,255) sostenido toda la ráfaga; cadencia medida en el audio: 106 ms
          daño recibido         ráfaga de ~250 ms con el motor pequeño a 255 y el grande AL AZAR 0-75
                                (cambia cada ~16 ms) -> por eso el disparo exige grande >= fire_l_min"""
        now = time.perf_counter()
        with self.cond:
            prev_l, prev_s = self.rumble
            self.rumble = (l, s)
            if self.attached is None:
                return None
            p = self.config.profile(self.attached)
            rising = (prev_l, prev_s) == (0, 0) and (l or s)
            if self.fire_from_state(now, p) or self.state_live(now, p):
                # Disparos ("shot") y daño ("dmg") llegan del estado del juego: el rumble ya no decide
                # nada aquí (los hápticos lo siguen traduciendo en unds.py), salvo las armas montadas.
                if self.auto_active:
                    self.end_auto("estado del juego")
                return self.mounted_rumble(now, p, prev_l, prev_s, l, s)
            fire_now = s >= p.fire_s_min and p.fire_l_min <= l <= p.fire_l_max
            fire_before = prev_s >= p.fire_s_min and p.fire_l_min <= prev_l <= p.fire_l_max
            if self.auto_active and not (fire_now and l in p.auto_l):
                self.end_auto("fin del rumble")
            if fire_now and not fire_before:
                r2_recent = (self.buttons & BTN_R2) or now - self.r2_down_at <= p.fire_r2_window_s
                if r2_recent and not self.calibrating:
                    self.last_combat = now
                    self.counts["kick"] += 1
                    if l in p.auto_l:
                        self.auto_active, self.auto_shots = True, 1
                        self.auto_next = now + p.auto_period_s
                        self.start_kick(now, p.auto_kick)
                        self.counts["ráfagas"] += 1
                        self.cond.notify_all()
                        return "fire_auto"
                    self.start_kick(now, p.kick)
                    self.cond.notify_all()
                    return "fire"
                self.counts["fire_sin_r2"] += 1
                return "other"
            damage_now = s >= p.fire_s_min and l < p.fire_l_min
            if damage_now:
                new_hit = (prev_l, prev_s) == (0, 0) or now - self.last_damage_rx > 0.08
                self.last_damage_rx = now
                if new_hit and p.damage_on and self.death_phase is None:
                    self.damage_level = min(1.0, self.damage_now(now, p) + p.damage_step)
                    self.damage_t = now
                    self.last_combat = now
                    self.counts["daño"] += 1
                    self.cond.notify_all()
                    return "hit"
                return None
            if rising:
                self.counts["rumble_otro"] += 1
                return "other"
            return None

    # ---- armas montadas (Día 8b) -----------------------------------------------------------------
    def mounted_rumble(self, now: float, p: Profile, prev_l: int, prev_s: int, l: int, s: int) -> str | None:
        """Bajo cond, con estado real. Firmas medidas en la torreta del jeep (U1, grabación 2026-10-25
        11:55, 290-330 s): R2 -> (114,255) a los 3-6 ms en 11 de 11 pulsaciones, sostenido y bajando
        mientras se dispara, (0,0) al soltar; L2 -> pulso de 255 a los 4-5 ms en 7 de 16 (las otras 9
        no dispararon) y una rampa de ~1,4 s al explotar. Fuera de la escena: 1 de 57 pulsaciones con
        rumble < 40 ms y ninguna con estas firmas. 'mount_r2' | 'mount_r2_single' | 'mount_r2_light' | 'mount_l2'
        | None. Cada pulsación de R2 se clasifica una sola vez."""
        if not p.mounted_on or self.calibrating or self.death_phase is not None or self.weapon != "0x0" \
                or self.player_active is False:
            self.end_mount_auto("fuera de arma montada")
            return None
        if self.mount_auto and (not (self.buttons & BTN_R2) or s < p.fire_s_min):
            self.end_mount_auto("fin del rumble" if self.buttons & BTN_R2 else "R2 suelto")
        r2_age, l2_age = now - self.r2_down_at, now - self.l2_down_at
        r2_new = bool(self.buttons & BTN_R2) and 0 <= r2_age <= p.mounted_window_s \
            and self.mount_r2_press != self.r2_down_at
        l2_new = bool(self.buttons & BTN_L2) and 0 <= l2_age <= p.mounted_window_s
        pulse = l >= 230 and l > prev_l          # disparo único: granada / lanzagranadas
        if pulse and l2_new and (not r2_new or l2_age <= r2_age):
            self.mounted_until = now + p.mounted_hold_s
            self.mount_l2 = "single"
            self.start_lkick(now, self.mounted_profile(p.mounted_l2_class).kick)
            self.last_combat = now
            self.counts["montada_l2"] += 1
            self.cond.notify_all()
            return "mount_l2"
        if pulse and r2_new:
            # Moto acuática (Elena, 2026-10-25 19:21, 232-293 s): R2 -> (255,0) a los 5-7 ms en los 2
            # disparos; las otras 4 pulsaciones no dispararon (recarga) y no dieron rumble.
            self.mounted_until = now + p.mounted_hold_s
            self.mount_r2, self.mount_r2_cls, self.mount_r2_press = "single", p.mounted_r2_single_class, self.r2_down_at
            self.end_mount_auto("disparo único")
            self.start_kick(now, self.mounted_profile(p.mounted_r2_single_class).kick)
            self.last_combat = now
            self.counts["montada_r2_unico"] += 1
            self.cond.notify_all()
            return "mount_r2_single"
        firing = r2_new and not self.mount_auto and s >= p.fire_s_min and l >= p.fire_l_min \
            and (l > prev_l or prev_s < p.fire_s_min)
        if firing and l not in p.auto_l and l <= p.fire_l_max:
            # Moto acuática, 2.ª parte (Elena con pistola, 2026-10-25 22:53, 2380-2830 s): R2 -> (76,255) a
            # los 2-6 ms, (76,0) a los ~120 ms y (0,0) a los ~185 ms aunque R2 siga pulsado: la firma de la
            # pistola en la mano, no la torreta (114,255 sostenido mientras se dispara).
            self.mounted_until = now + p.mounted_hold_s
            self.mount_r2, self.mount_r2_cls, self.mount_r2_press = "single", p.mounted_r2_light_class, self.r2_down_at
            self.end_mount_auto("disparo único (pistola)")
            self.start_kick(now, self.mounted_profile(p.mounted_r2_light_class).kick)
            self.last_combat = now
            self.counts["montada_r2_pistola"] += 1
            self.cond.notify_all()
            return "mount_r2_light"
        if firing and l in p.auto_l:
            self.mount_r2_press = self.r2_down_at
            self.mounted_until = now + p.mounted_hold_s
            self.mount_r2 = "auto"
            self.mount_auto, self.mount_shots = True, 1
            self.mount_next = now + p.mounted_period_s
            self.start_kick(now, self.mounted_profile(p.mounted_r2_class).auto_kick)
            self.last_combat = now
            self.counts["montada_r2"] += 1
            self.cond.notify_all()
            return "mount_r2"
        return None

    def mounted_profile(self, cls: str) -> Profile:
        return self.config.weapon_profile(self.attached or "default", "", cls)

    def mounted(self, now: float, p: Profile) -> bool:
        return p.mounted_on and now < self.mounted_until and self.weapon == "0x0" and self.state_live(now, p) \
            and self.player_active is not False

    def end_mount_auto(self, why: str) -> None:
        if self.mount_auto:
            self.mount_auto = False
            if self.args.verbose:
                self.say(f"FIRE  torreta montada: ráfaga terminada ({why}): {self.mount_shots} disparos")

    def start_lkick(self, start: float, seq: tuple) -> None:
        self.lkick_t, self.lkick_seq = start, seq
        self.lkick_until = start + sum(d for _, d in seq)

    def lkick_phase(self, now: float) -> tuple[Effect, float]:
        end = self.lkick_t
        for effect, duration in self.lkick_seq:
            end += duration
            if now < end:
                return effect, end
        return self.lkick_seq[-1][0], self.lkick_until

    # ---- armas (Día 7) ---------------------------------------------------------------------------
    def weapon_class(self, wid: str) -> str:
        """Clase del arma: la de [arma ...] si la fija; si no, por lo visto en el juego."""
        info = self.config.weapons.get(wid)
        if info is not None and info.cls:
            return info.cls
        cap = self.weapon_caps.get(wid, -1)
        if cap >= 1_000_000:
            return "torreta"                     # munición infinita (U1: 100000000)
        if cap == 1:
            return "lanzagranadas"
        if wid in self.auto_weapons:
            return "automatica"
        return "pistola"

    def weapon_name(self, wid: str) -> str:
        info = self.config.weapons.get(wid)
        return info.name if info is not None and info.name else wid

    def is_automatic(self, wid: str) -> bool:
        info = self.config.weapons.get(wid)
        if info is not None and info.automatic is not None:
            return info.automatic
        fixed = self.config.class_automatic(self.weapon_class(wid))
        return fixed if fixed is not None else wid in self.auto_weapons

    def weapon_profile(self, wid: str) -> Profile:
        """Perfil de gatillos para el arma (bajo cond)."""
        return self.config.weapon_profile(self.attached or "default", wid, self.weapon_class(wid))

    def announce_weapon(self, wid: str) -> None:
        cls = self.weapon_class(wid)
        if self.weapons_seen.get(wid) != cls:
            self.weapons_seen[wid] = cls
            info = self.config.weapons.get(wid)
            origin = "del .ini" if info is not None and info.cls else "deducida"
            cap = self.weapon_caps.get(wid)
            self.say(f"ARMA  {wid} = {self.weapon_name(wid)}: clase {cls} ({origin}"
                     f"{f', cargador {cap}' if cap and cap < 1_000_000 else ''})")

    def unarmed(self, now: float, p: Profile) -> bool:
        return self.state_live(now, p) and self.weapon == "0x0" and now - self.weapon_since >= p.unarmed_s

    def in_cutscene(self, now: float, p: Profile) -> bool:
        """Bajo cond: cinemática = estado real vivo, el juego no actualiza al jugador (ups = 0 en el
        último refresco) y vida > 0 (la muerte va aparte). La pausa/menú/carga (luz azul sostenida)
        se decide antes en desired(); un destello azul corto no corta la cinemática."""
        return p.cutscene_on and self.player_active is False and self.state_live(now, p) and self.hp > 0

    def start_kick(self, start: float, seq: tuple) -> None:
        """Bajo cond: arranca un kick (sus fases) en `start`."""
        self.kick_t, self.kick_seq = start, seq
        self.kick_until = start + sum(d for _, d in seq)

    def kick_phase(self, now: float) -> tuple[Effect, float]:
        """(efecto de la fase en curso, fin de esa fase). Bajo cond, con now < kick_until."""
        end = self.kick_t
        for effect, duration in self.kick_seq:
            end += duration
            if now < end:
                return effect, end
        return self.kick_seq[-1][0], self.kick_until

    def damage_now(self, now: float, p: Profile) -> float:
        """Vida perdida estimada (0-1): sube con cada golpe, se mantiene damage_hold_s sin recibir daño
        y luego se recupera linealmente en damage_recover_s (como la regeneración de Uncharted).
        [HIPÓTESIS] es una estimación por golpes; la vida real llega con el estado del juego (Días 5-6)."""
        elapsed = now - self.damage_t - p.damage_hold_s
        if elapsed <= 0:
            return self.damage_level
        return max(0.0, self.damage_level - elapsed / p.damage_recover_s)

    def end_auto(self, why: str) -> None:
        if self.auto_active:
            self.auto_active = False
            if self.args.verbose:
                self.say(f"FIRE  ráfaga terminada ({why}): {self.auto_shots} disparos")

    def game_state(self, ev: dict) -> None:
        """gs: vida (x1000), herido, arma, munición. Refrescos (re) con ups/wps cada 0,5 s."""
        now = time.perf_counter()
        with self.cond:
            if self.attached is None:
                return
            p = self.config.profile(self.attached)
            if ev.get("re"):
                # ups = actualizaciones del jugador desde el refresco anterior (~0,5 s). 0 con la luz del
                # juego normal = cinemática [VERIFICADO 2026-10-23: 193427 543-581 s y 182744 305-349 s,
                # audio sonando, sin rumble]; 0 con luz azul = pausa/menú/carga (ya lo cubre menu_rgb).
                active = int(ev.get("ups", 0)) > 0
                if active != self.player_active:
                    if self.player_active is not None or not active:
                        blue = self.led_rgb == p.menu_rgb
                        self.say("STATE jugador activo de nuevo" if active else
                                 "STATE el juego no actualiza al jugador (ups=0): "
                                 + ("pausa/menú/carga" if blue else "cinemática -> gatillos " +
                                    f"{p.cutscene_l2}/{p.cutscene_r2}" if p.cutscene_on else "cinemática"))
                    self.player_active = active
                    if not active and self.led_rgb != p.menu_rgb and self.mounted_until > now:
                        self.mounted_until = -1e9          # cinemática: se acabó el arma montada
                        self.mount_r2 = self.mount_l2 = None; self.mount_r2_cls = ""
                        self.end_mount_auto("cinemática")
                    self.cond.notify_all()
            hp = int(ev.get("hp", -1))
            if hp < 0:
                return                            # no es una vida (objeto equivocado): sin estado real
            first = now - self.gs_at >= 2.0
            self.gs_at = now
            old_hp, self.hp = self.hp, hp / 1000.0
            self.injured = bool(ev.get("inj"))
            weapon = str(ev.get("w", "0x0"))
            self.clip, self.cap, self.reserve = int(ev.get("clip", -1)), int(ev.get("cap", -1)), int(ev.get("res", -1))
            if weapon != "0x0" and self.cap > 0:
                self.weapon_caps[weapon] = self.cap
            if weapon != self.weapon:
                self.weapon, self.weapon_since = weapon, now
                if weapon != "0x0":
                    self.announce_weapon(weapon)
                    self.mounted_until = -1e9          # arma en la mano: se acabó el arma montada
                    self.mount_r2 = self.mount_l2 = None; self.mount_r2_cls = ""
                    self.end_mount_auto("arma en la mano")
                self.cond.notify_all()
            if first and p.state_on:
                self.say(f"STATE estado real del juego: vida {self.hp * 100:.0f}%, arma {self.weapon}"
                         f"{' — disparos por evento shot' if self.fire_from_state(now, p) else ''}")
            if hp == 0 and old_hp > 0:
                self.hp_zero_at = now
                self.counts["vida_0"] += 1
                if p.state_on and p.death_hold and self.death_phase != "hold":
                    self.start_death(now, "STATE muerte (vida 0): rojo fijo y gatillos libres hasta reaparecer")
            elif hp >= 1000 and self.death_phase == "hold" and self.hp_zero_at is not None \
                    and self.hp_zero_at >= self.death_t - 0.01:
                # La reaparición pone la vida a 1.0 (tras la muerte puede regenerarse antes, sin llegar)
                self.end_death(now, f"STATE reaparición (vida 100 %) tras {now - self.death_t:.1f} s")
            if abs(self.hp - old_hp) > 1e-4 or first:
                self.cond.notify_all()

    def shot(self, ev: dict) -> str | None:
        """shot: el arma del jugador gastó una bala. 'fire' o 'fire_auto' (para el háptico) si los
        disparos vienen del estado del juego; None si no (fire_source = rumble)."""
        now = time.perf_counter()
        with self.cond:
            if self.attached is None:
                return None
            p = self.config.profile(self.attached)
            self.counts["shot"] += 1
            if not self.fire_from_state(now, p) or self.calibrating:
                return None
            weapon = str(ev.get("w", "0x0"))
            cap = int(ev.get("cap", -1)) if isinstance(ev.get("cap"), int) else -1
            if cap > 0:
                self.weapon_caps[weapon] = cap
            gap = now - self.last_shot_t if weapon == self.last_shot_w else 1e9
            if gap <= p.auto_gap_s and weapon not in self.auto_weapons:
                self.auto_weapons.add(weapon)
                self.say(f"FIRE  arma {weapon} automática ({gap * 1000:.0f} ms entre disparos)")
                info = self.config.weapons.get(weapon)
                if info is None or not info.cls:
                    self.weapons_seen.pop(weapon, None)        # su clase deducida puede cambiar
            if weapon not in self.weapons_seen:
                self.announce_weapon(weapon)
            automatic = self.is_automatic(weapon)
            wp = self.weapon_profile(weapon)
            self.last_shot_t, self.last_shot_w = now, weapon
            self.last_combat = now
            self.counts["kick"] += 1
            if automatic:
                self.counts["disparos_auto"] += 1
            self.start_kick(now, wp.auto_kick if automatic else wp.kick)
            self.cond.notify_all()
            return "fire_auto" if automatic else "fire"

    def damage(self, ev: dict) -> str | None:
        """dmg: daño real recibido (tipo, cantidad, vida antes y origen). 'hit' para el háptico."""
        now = time.perf_counter()
        with self.cond:
            if self.attached is None:
                return None
            p = self.config.profile(self.attached)
            self.counts["daño"] += 1
            if not p.state_on or self.death_phase is not None:
                return None
            self.damage_t = now
            self.last_combat = now
            self.cond.notify_all()
            return "hit"

    # ---- decisión -------------------------------------------------------------------------------
    def desired(self, now: float) -> dict[str, object] | None:
        """{"L": Effect, "R": Effect, "RGB": (r,g,b,brillo) o None}; None = sin juego (reset)."""
        if self.attached is None:
            return None
        p = self.config.profile(self.attached)
        if self.death_phase == "hold" and now - self.death_t >= p.death_hold_s:
            self.death_phase, self.fade_t = "fade", now                # sin reaparición detectada
        if self.death_phase == "fade" and now - self.fade_t >= p.death_fade_s:
            self.death_phase = None
        if self.auto_active:
            if not (self.buttons & BTN_R2) and now - self.r2_up_at > 0.15:
                self.end_auto("R2 suelto")
            while self.auto_active and now >= self.auto_next:   # un golpe de gatillo y de háptico por disparo
                self.start_kick(self.auto_next, p.auto_kick)
                self.auto_next += p.auto_period_s
                self.auto_shots += 1
                if self.on_event:
                    self.on_event("shot")
        if self.mount_auto:
            if not (self.buttons & BTN_R2) and now - self.r2_up_at > 0.05:
                self.end_mount_auto("R2 suelto")
            while self.mount_auto and now >= self.mount_next:   # torreta montada: golpe y háptico por disparo
                self.start_kick(self.mount_next, self.mounted_profile(p.mounted_r2_class).auto_kick)
                self.mount_next += p.mounted_period_s
                self.mount_shots += 1
                self.mounted_until = now + p.mounted_hold_s
                self.last_combat = now
                if self.on_event:
                    self.on_event("mount_shot")
        mounted = self.mounted(now, p)
        if not mounted and now >= self.mounted_until and (self.mount_r2 or self.mount_l2):
            self.mount_r2 = self.mount_l2 = None              # el modo caducó: los papeles se aprenden de nuevo
            self.mount_r2_cls = ""
        roles = (self.mount_r2, self.mount_r2_cls, self.mount_l2) if mounted else None
        if roles != self.mounted_shown:
            if roles:
                r2 = {"auto": p.mounted_r2_class, "single": self.mount_r2_cls or p.mounted_r2_single_class}.get(self.mount_r2, "-")
                l2 = p.mounted_l2_class if self.mount_l2 == "single" else "apuntar"
                self.say(f"ARMA  montada: R2 = {r2}, L2 = {l2} (sin arma en la mano; por la firma del rumble)")
            elif self.mounted_shown:
                self.say("ARMA  montada: fin")
            self.mounted_shown = roles
        if (self.buttons & BTN_L2) and now - self.l2_down_at >= p.aim_arm_s:
            self.last_combat = now                                      # apuntar un rato = en combate
        armed = p.context_always or now - self.last_combat <= p.combat_s
        if armed != self.armed_shown:
            if self.armed_shown is not None or armed:
                self.say("GATILLOS en combate (resistencia activa)" if armed else
                         f"GATILLOS en calma: {p.combat_s:.0f} s sin disparar, apuntar ni recibir daño")
            self.armed_shown = armed
        is_blue = self.led_rgb == p.menu_rgb
        menu = is_blue and now - self.led_since >= p.menu_debounce_s and self.death_phase != "hold"
        # color del juego sin el parpadeo azul corto (reaparición, recarga del punto de control)
        game_rgb = self.led_prev if (is_blue and not menu and self.led_prev) else self.led_rgb
        if self.calibrating:
            left, right = parse_effect("OFF"), p.r2_aim
        elif self.death_phase == "hold":
            left, right = p.death_l2, p.death_r2
        elif menu:
            left, right = p.menu_l2, p.menu_r2
        elif self.in_cutscene(now, p):
            left, right = p.cutscene_l2, p.cutscene_r2
        elif mounted:
            if now < self.lkick_until:
                left = self.lkick_phase(now)[0]
            else:
                left = p.mounted_l2 if self.mount_l2 == "single" else p.l2      # granadas | peso de apuntar
            if now < self.kick_until:
                right = self.kick_phase(now)[0]
            elif self.mount_r2 == "single":
                sp = self.mounted_profile(self.mount_r2_cls or p.mounted_r2_single_class)
                right = sp.r2_aim if self.buttons & BTN_L2 else sp.r2_idle
            elif self.mount_r2 == "auto":
                right = self.mounted_profile(p.mounted_r2_class).r2_idle
            else:
                right = p.unarmed_r2
        elif self.unarmed(now, p):
            left, right = p.unarmed_l2, p.unarmed_r2
        else:
            aiming = bool(self.buttons & BTN_L2)
            live = self.state_live(now, p)
            # Día 7: con estado real, los efectos del arma en la mano (clase + [arma ...])
            wp = self.weapon_profile(self.weapon) if live and self.weapon != "0x0" else p
            left = wp.l2 if armed else p.calm_l2
            if now < self.kick_until:
                right = self.kick_phase(now)[0]
            elif not armed:
                right = p.calm_r2
            elif live and self.cap > 0 and self.clip == 0:
                right = wp.empty_r2                                     # cargador vacío
            else:
                right = wp.r2_aim if aiming else wp.r2_idle
        rgb = None
        if p.led_game and game_rgb:
            shown = scale_rgb(game_rgb, p.led_gain)
            if self.death_phase == "hold":
                shown = scale_rgb(p.death_rgb, p.led_gain)
            elif self.death_phase == "fade":
                shown = lerp_rgb(scale_rgb(p.death_rgb, p.led_gain), shown, (now - self.fade_t) / p.death_fade_s)
            else:
                if self.state_live(now, p):                                # vida real del juego
                    if p.health_led == "state" and self.led_healthy:
                        shown = scale_rgb(self.led_healthy, p.led_gain)   # sin el degradado del juego
                        damage = max(0.0, 1.0 - self.hp) ** p.health_curve if p.damage_on else 0.0
                    else:
                        damage = 0.0          # health_led = game: el color del juego ya sigue la vida
                else:
                    damage = self.damage_now(now, p)                       # estimación por golpes
                if not p.damage_on:
                    pass
                elif now - self.damage_t < p.damage_flash_s:
                    shown = p.damage_rgb                                   # destello del golpe
                elif damage > 0:
                    shown = lerp_rgb(shown, p.damage_rgb, damage)
            rgb = (*shown, p.led_brightness)
        if now < self.alert_until:
            lit = int((now - self.alert_t) / ALERT_BLINK_S) % 2 == 0
            rgb = (255, 0, 0, 255) if lit else (0, 0, 0, 255)
        return {"L": left, "R": right, "RGB": rgb}

    def next_deadline(self, now: float) -> float:
        """Segundos hasta el próximo cambio que no llega como evento (bajo cond)."""
        if self.attached is None:
            return 0.25
        p = self.config.profile(self.attached)
        wait = 0.25
        if self.kick_until > now:
            wait = min(wait, self.kick_phase(now)[1] - now)          # cambio de fase o fin del kick
        if self.auto_active:
            wait = min(wait, self.auto_next - now)
        if self.mount_auto:
            wait = min(wait, self.mount_next - now)
        if self.lkick_until > now:
            wait = min(wait, self.lkick_phase(now)[1] - now)
        if now < self.mounted_until:
            wait = min(wait, self.mounted_until - now + 0.001)
        if (self.buttons & BTN_L2) and now - self.l2_down_at < p.aim_arm_s:
            wait = min(wait, self.l2_down_at + p.aim_arm_s - now)
        if not p.context_always and 0 <= now - self.last_combat <= p.combat_s:
            wait = min(wait, self.last_combat + p.combat_s - now + 0.01)
        if self.state_live(now, p):
            if self.weapon == "0x0" and now - self.weapon_since < p.unarmed_s:
                wait = min(wait, self.weapon_since + p.unarmed_s - now + 0.001)
            if now - self.damage_t < p.damage_flash_s:
                wait = min(wait, self.damage_t + p.damage_flash_s - now)
            if self.kick_until <= now and now - self.gs_at < 2.0:
                wait = min(wait, self.gs_at + 2.0 - now + 0.01)       # el estado caduca: vuelta a la estimación
        elif self.damage_now(now, p) > 0:
            wait = min(wait, 0.03)                                    # degradado del tinte de daño
        if self.led_rgb == p.menu_rgb and now - self.led_since < p.menu_debounce_s:
            wait = min(wait, self.led_since + p.menu_debounce_s - now)
        if now < self.alert_until:
            wait = min(wait, ALERT_BLINK_S - (now - self.alert_t) % ALERT_BLINK_S + 0.001)
        if self.death_phase == "hold":
            wait = min(wait, self.death_t + p.death_hold_s - now)
        elif self.death_phase == "fade":
            wait = min(wait, 0.03)                                    # ~33 pasos/s de degradado
        return max(wait, 0.0005)

    # ---- hilo emisor ----------------------------------------------------------------------------
    def start(self) -> None:
        self.thread.start()
        threading.Thread(target=self.status_loop, name="dsx-status", daemon=True).start()

    def close(self) -> None:
        self.stop_event.set()
        with self.cond:
            self.stop = True
            self.cond.notify_all()
        self.thread.join(timeout=1.0)
        if self.client:
            if self.controller is not None:
                self.client.send([self.client.reset(self.controller)])
            self.client.close()

    def refresh_controller(self, now: float) -> None:
        if not self.client:
            self.controller = 0
            return
        wanted = self.config.dsx["controller"].strip().lower()
        resp = self.client.status(timeout=0.3)
        self.status_at = now
        if resp is None:
            if self.backend in ("hid", "port"):  # HidClient / PortClient ya explican el problema
                if self.controller is None:
                    self.controller = 0
                return
            if not self.no_reply_warned:
                self.no_reply_warned = True
                self.say(f"DSX  NO RESPONDE en 127.0.0.1:{self.client.port} ({self.client.port_source}). "
                         f"¿DSX abierto? Sigo enviando a ciegas al mando {self.controller or 0}.")
            if self.controller is None:
                self.controller = 0 if wanted == "auto" else int(wanted)
            return
        if self.no_reply_warned:
            self.say("DSX  vuelve a responder")
        self.no_reply_warned = False
        if not (resp.data and isinstance(resp.data.get("Devices"), list)):
            return                     # respuesta a otro paquete, sin lista de mandos
        index, desc = pick_controller(resp)
        new = (index if index is not None else 0) if wanted == "auto" else int(wanted)
        if new != self.controller:
            rtt = f"{resp.rtt_ms:.1f} ms" if resp.rtt_ms is not None else "n/d"
            self.say(f"{self.label}  mando {new}: {desc}" + (f" (RTT {rtt})" if self.backend == "dsx" else ""))
            self.controller = new
            with self.cond:
                self.sent.clear()

    def status_loop(self) -> None:
        """Estado de DSX en un hilo aparte: una espera de respuesta nunca retrasa un kick."""
        every = float(self.config.dsx["status_s"])
        while not self.stop_event.is_set():
            self.refresh_controller(time.perf_counter())
            self.stop_event.wait(every)

    def run(self) -> None:
        reassert = float(self.config.dsx["reassert_s"])
        min_interval = float(self.config.dsx["min_interval_ms"]) / 1000.0
        was_attached = False
        while True:
            with self.cond:
                while True:
                    if self.stop:
                        return
                    now = time.perf_counter()
                    want = self.desired(now)
                    if want is None:
                        if was_attached:
                            break                      # hay que devolver el mando al perfil
                        self.cond.wait(0.25)
                        continue
                    pending = any(self.sent.get(k) != v for k, v in want.items())
                    stale = now - self.sent_at >= reassert
                    if pending or stale:
                        break
                    timeout = min(self.next_deadline(now), self.sent_at + reassert - now)
                    self.cond.wait(max(timeout, 0.0005))
                snapshot = dict(self.sent)
            if want is None:
                self.transmit([("reset", None)], now, {"reset": "ResetToUserSettings"})
                with self.cond:
                    self.sent.clear()
                was_attached = False
                continue
            was_attached = True
            if now - self.sent_at < min_interval:
                time.sleep(min_interval - (now - self.sent_at))
                now = time.perf_counter()
                with self.cond:
                    want = self.desired(now) or want
            items = [(k, v) for k, v in want.items() if v is not None and (stale or snapshot.get(k) != v)]
            if items:
                self.transmit(items, now, {k: str(v) for k, v in items}, quiet=stale and not pending)
            with self.cond:
                self.sent.update(want)
                self.sent_at = now

    def transmit(self, items: list[tuple[str, object]], now: float, log: dict, quiet: bool = False) -> None:
        index = self.controller or 0
        instructions = []
        for key, value in items:
            if key == "L":
                instructions.append(DSXClient.trigger(index, LEFT, value))
            elif key == "R":
                instructions.append(DSXClient.trigger(index, RIGHT, value))
            elif key == "RGB":
                instructions.append(DSXClient.rgb(index, *value))
            elif key == "reset":
                instructions.append(DSXClient.reset(index))
        self.counts["paquetes"] += 1
        if self.client:
            self.client.send(instructions)
        if self.recorder:
            self.recorder({"rx_us": time.perf_counter_ns() // 1000, "wall": round(time.time(), 6), "src": "dsx",
                           "dsx": log})
        if quiet or all(k == "RGB" for k, _ in items) or getattr(self.args, "no_dsx", False):
            return          # re-afirmación periódica, solo lightbar (degradados) o unds.py --no-dsx: en el JSONL
        if self.args.verbose or any(k in ("L", "reset") for k, _ in items) or \
                (dict(items).get("R") is not None and not self.args.quiet_kick):
            text = " ".join(f"{k}={v}" for k, v in log.items())
            self.say(f"{self.label}  -> {text}")


class TriggerCompanion(Listener):
    def __init__(self, args: argparse.Namespace):
        super().__init__(args)
        self.config = Config(Path(args.config), getattr(args, "backend", None))
        self.config.load()
        self.write_lock = threading.Lock()
        self.engine = Engine(self.config, args, self.say)
        self.engine.recorder = self.write
        self.active_pid: int | None = None
        self.game = "default"
        self.config_checked = 0.0
        self.calib: list[tuple[float, int]] = []   # (t, r2a) mientras R2 sube
        self.calib_dwell: Counter = Counter()

    def write(self, item: dict) -> None:
        with self.write_lock:
            super().write(item)

    # ---- eventos del port -----------------------------------------------------------------------
    def on_hello(self, em: Emitter, ev: dict) -> None:
        super().on_hello(em, ev)
        if self.active_pid == em.pid or em.ended:
            return
        self.active_pid = em.pid
        game = em.exe if em.exe in ("u1", "u2", "u3") else "other"
        self.game = game
        port_fx = self.engine.backend == "port" and ev.get("fx") == 1
        if self.engine.backend == "port" and self.engine.client:
            self.engine.client.retarget(em.source, em.pid, ev)
        if "led" not in ev:
            self.say("AVISO el port no trae el parche 0003: el LED lo pelean SDL y UNDS y no hay l2a/r2a.")
        elif ev.get("led") == 1 and self.config.profile(game).led_game and not port_fx:
            # con el canal fx el port ya no manda el color del juego a SDL mientras el companion lleva la lightbar
            self.say("AVISO el port corre sin UNDS_LED=0: SDL y UNDS se pisarán la lightbar.")
        if ev.get("rumble") == 1 and not ev.get("replay"):
            self.say("AVISO rumble de SDL activo: pon UNDS_RUMBLE=0 en el launcher. Mientras SDL manda rumble, "
                     "desactiva los hápticos por audio del mando (unds.py no se sentirá).")
        if "trig" in ev:
            self.say(f"nota  umbrales de pulsación del port L2,R2 = {ev['trig']} (de 255)")
        self.say(f"{self.engine.label}  juego {game} (pid {em.pid}): perfil [{game if game in self.config.profiles else 'default'}]"
                 f"{' — MODO CALIBRACIÓN' if self.args.calibrate else ''}")
        self.engine.attach(game)

    def on_fx(self, em: Emitter, ev: dict) -> None:
        super().on_fx(em, ev)
        if em.pid == self.active_pid and self.engine.backend == "port" and self.engine.client:
            self.engine.client.on_ack(ev)

    def emitter_for(self, ev: dict, source: str, now: float) -> Emitter | None:
        em = super().emitter_for(ev, source, now)
        if em is not None and em.pid == self.active_pid and self.engine.backend == "port" and self.engine.client:
            self.engine.client.note_alive()    # el port vive: soltar no necesita rescate por HID
        return em

    def on_bye(self, em: Emitter, ev: dict) -> None:
        super().on_bye(em, ev)
        if em.pid == self.active_pid:
            self.active_pid = None
            self.engine.detach(f"bye {em.ended}")

    def on_pad(self, em: Emitter, ev: dict) -> None:
        super().on_pad(em, ev)
        if em.pid != self.active_pid:
            return
        buttons = int(ev.get("b", 0))
        self.engine.pad(buttons)
        if self.args.calibrate and "r2a" in ev:
            self.calibrate_sample(int(ev["r2a"]))

    def on_rumble(self, em: Emitter, ev: dict) -> None:
        super().on_rumble(em, ev)
        if em.pid != self.active_pid or ev.get("re"):
            return
        kind = self.engine.rumble_event(int(ev.get("l", 0)), int(ev.get("s", 0)))
        self.rumble_hook(em, ev, kind)
        if kind == "fire" and self.args.verbose:
            self.say(f"FIRE  rumble ({ev.get('l')},{ev.get('s')}) -> kick")
        elif kind == "fire_auto" and self.args.verbose:
            self.say(f"FIRE  arma automática ({ev.get('l')},{ev.get('s')}) -> ráfaga")
        elif kind == "hit" and self.args.verbose:
            self.say(f"HIT   daño recibido ({ev.get('l')},{ev.get('s')})")
        elif kind == "other" and self.args.verbose:
            self.say(f"rumble sin firma de disparo ({ev.get('l')},{ev.get('s')})")

    def rumble_hook(self, em: Emitter, ev: dict, kind: str | None) -> None:
        """Cada rumble del juego activo (sin refrescos), ya clasificado: 'fire', 'other' o None.
        unds.py lo usa para los hápticos."""

    def on_led(self, em: Emitter, ev: dict) -> None:
        super().on_led(em, ev)
        if em.pid == self.active_pid:
            self.engine.led((int(ev.get("r", 0)), int(ev.get("g", 0)), int(ev.get("b", 0))))

    # ---- estado del juego (Día 5) ---------------------------------------------------------------
    def on_gs(self, em: Emitter, ev: dict) -> None:
        super().on_gs(em, ev)
        if em.pid == self.active_pid:
            self.engine.game_state(ev)

    def on_shot(self, em: Emitter, ev: dict) -> None:
        super().on_shot(em, ev)
        if em.pid != self.active_pid:
            return
        kind = self.engine.shot(ev)
        self.shot_hook(em, ev, kind)
        if kind and self.args.verbose:
            self.say(f"FIRE  shot {ev.get('w')} cargador {ev.get('clip')}/{ev.get('cap')} -> "
                     f"{'ráfaga' if kind == 'fire_auto' else 'kick'}")

    def on_dmg(self, em: Emitter, ev: dict) -> None:
        super().on_dmg(em, ev)
        if em.pid != self.active_pid:
            return
        kind = self.engine.damage(ev)
        self.damage_hook(em, ev, kind)

    def shot_hook(self, em: Emitter, ev: dict, kind: str | None) -> None:
        """Cada shot del juego activo: 'fire', 'fire_auto' o None (no decide). unds.py: hápticos."""

    def damage_hook(self, em: Emitter, ev: dict, kind: str | None) -> None:
        """Cada dmg del juego activo: 'hit' o None. unds.py: háptico de daño."""

    # ---- calibración ----------------------------------------------------------------------------
    def calibrate_sample(self, r2a: int) -> None:
        """Acumula tiempo de permanencia por valor de r2a mientras el gatillo está entre 8 y 247:
        donde está la pared del efecto, el dedo se queda quieto antes de romperla."""
        now = time.perf_counter()
        if self.calib:
            t0, v0 = self.calib[-1]
            if 8 <= v0 <= 247:
                self.calib_dwell[v0 // 4 * 4] += now - t0
        self.calib.append((now, r2a))
        del self.calib[:-2]

    def calibration_report(self) -> None:
        if not self.args.calibrate:
            return
        total = sum(self.calib_dwell.values())
        print("\nCalibración de R2 (tiempo con el dedo quieto por tramo de recorrido, 0-255):")
        if total < 1.0:
            print("  muy pocos datos: aprieta R2 despacio contra la resistencia, unas 10 veces.")
            return
        for value, dwell in sorted(self.calib_dwell.items()):
            bar = "#" * int(60 * dwell / max(self.calib_dwell.values()))
            print(f"  {value:3d}-{value + 3:3d} {dwell:6.2f} s {bar}")
        # La pared es el tramo contiguo alrededor del máximo con >= 10 % de su permanencia. El umbral
        # debe quedar por ENCIMA de su borde superior: con el dedo apoyado en la pared no hay disparo,
        # y al romperla el dedo cruza el umbral en pocos ms camino del tope.
        peak_bin, peak = max(self.calib_dwell.items(), key=lambda kv: kv[1])
        low = high = peak_bin
        while self.calib_dwell.get(low - 4, 0.0) >= 0.1 * peak:
            low -= 4
        while self.calib_dwell.get(high + 4, 0.0) >= 0.1 * peak:
            high += 4
        top = high + 3
        suggestion = min(250, top + 10)
        print(f"\n  Pared de {self.config.profile(self.game).r2_aim}: r2a ~ {low}-{top} (máximo en {peak_bin}-{peak_bin + 3}).")
        print(f"  Umbral por encima de la pared (dispara al romperla, no al apoyarse): UNDS_TRIGGER=30,{suggestion}")

    # ---- bucle ----------------------------------------------------------------------------------
    def periodic(self) -> None:
        super().periodic()
        now = time.perf_counter()
        if self.active_pid is not None:
            em = self.emitters.get(self.active_pid)
            if em and em.silent_reported:
                self.active_pid = None
                self.engine.detach("silencio")
        if now - self.config_checked >= 1.0:
            self.config_checked = now
            if self.config.changed():
                try:
                    self.config.load()
                    with self.engine.cond:
                        self.engine.sent.clear()
                        self.engine.cond.notify_all()
                    self.say(f"config recargada de {self.config.path}")
                except ValueError as error:
                    self.say(f"config NO recargada ({error}); sigo con la anterior")
                    self.config.mtime = self.config.path.stat().st_mtime

    def summary(self) -> None:
        super().summary()
        counts = self.engine.counts
        print(f"Gatillos ({self.engine.label}): paquetes={counts['paquetes']} kicks={counts['kick']} "
              f"ráfagas={counts['ráfagas']} daño={counts['daño']} muertes={counts['muertes']} "
              f"firma-sin-R2={counts['fire_sin_r2']} rumble-sin-firma={counts['rumble_otro']}")
        if counts["shot"] or self.engine.gs_at > -1e8:
            autos = ", ".join(sorted(self.engine.auto_weapons)) or "-"
            print(f"     estado del juego: shot={counts['shot']} (automáticos {counts['disparos_auto']}) "
                  f"vida-0={counts['vida_0']} armas automáticas: {autos}")
            for wid, cls in sorted(self.engine.weapons_seen.items()):
                print(f"     arma {wid} = {self.engine.weapon_name(wid)}: clase {cls}")
        if counts["montada_r2"] or counts["montada_l2"] or counts["montada_r2_unico"]:
            print(f"     armas montadas: ráfagas R2={counts['montada_r2']} disparos únicos R2={counts['montada_r2_unico']} "
                  f"granadas L2={counts['montada_l2']}")
        if self.engine.client:
            c = self.engine.client
            extra = (f" respuestas={c.response_count()} ICMP-sin-escucha={c.reset_errors}"
                     if self.engine.backend == "dsx" else "")
            if self.engine.backend == "port":
                extra = f" acks={c.response_count()} rescates_HID={c.rescues}\n     {c.latency_text()}"
            print(f"     enviados={c.sent} errores_envío={c.send_errors}{extra}")
        self.calibration_report()

    def run(self) -> int:
        self.engine.start()
        try:
            return super().run()
        finally:
            self.engine.close()


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="UNDS: gatillos adaptativos y lightbar (por el port, HID directo o DSX).")
    parser.add_argument("--host", default="127.0.0.1", help="dirección donde escuchar el port (127.0.0.1)")
    parser.add_argument("--port", type=int, default=47111, help="puerto UDP del bus UNDS (47111)")
    parser.add_argument("--config", default=str(here / "unds_triggers.ini"), help="archivo .ini (unds_triggers.ini)")
    parser.add_argument("--out", default="recordings", help="carpeta de grabaciones (recordings)")
    parser.add_argument("--no-record", action="store_true", help="no grabar JSONL")
    parser.add_argument("--show", default="hello,bye", help="como unds_listen.py (hello,bye; añade led, gs, shot, dmg)")
    parser.add_argument("--stats-every", type=float, default=10.0, help="segundos entre líneas STATS (10)")
    parser.add_argument("--timeout", type=float, default=3.0, help="segundos de silencio = juego cerrado (3)")
    parser.add_argument("--calibrate", action="store_true", help="R2 = r2_aim fijo, sin kicks; mide la pared")
    parser.add_argument("--dry-run", action="store_true", help="no enviar nada al mando")
    parser.add_argument("--backend", choices=BACKENDS, help="port | hid | dsx (gana a [output] backend del .ini)")
    parser.add_argument("--verbose", "-v", action="store_true", help="imprime cada disparo y cada paquete")
    parser.add_argument("--quiet-kick", action="store_true", help="no imprimir cambios de R2")
    args = parser.parse_args(argv)
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    try:
        companion = TriggerCompanion(args)
    except ValueError as error:
        print(f"error en {args.config}: {error}", file=sys.stderr)
        return 2
    signal.signal(signal.SIGINT, signal.default_int_handler)
    return companion.run()


if __name__ == "__main__":
    sys.exit(main())
