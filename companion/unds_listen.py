#!/usr/bin/env python3
"""UNDS — escucha del bus de eventos del port (protocolo v1; eventos gs/shot/dmg desde el parche 0004).

Recibe los datagramas UDP que publica bb-probe.exe con UNDS_EVENTS=127.0.0.1:47111, los muestra
en vivo, los graba en JSONL y lleva estadísticas por emisor (un emisor = un proceso bb-probe; cada
cambio de juego o reinicio es un emisor nuevo).

    python unds_listen.py                       # 127.0.0.1:47111, graba en .\\recordings\\
    python unds_listen.py --show all            # también pad completo y audio (mucho texto)
    python unds_listen.py --show none --no-record

Línea grabada:  {"rx_us": <perf_counter en us>, "wall": <epoch s>, "src": "ip:port", "ev": {...}}
                ("raw" en lugar de "ev" si el datagrama no es JSON válido).
Solo biblioteca estándar (Python 3.10+). Ctrl+C termina e imprime el resumen.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import socket
import sys
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path

PROTOCOL = 1
EVENT_TYPES = ("hello", "bye", "rumble", "led", "pad", "aopen", "audio", "gs", "shot", "dmg", "fx")
# runtime_pad.c, enum BTN_*
BUTTONS = (
    (0x2, "L3"), (0x4, "R3"), (0x8, "OPTIONS"), (0x10, "UP"), (0x20, "RIGHT"), (0x40, "DOWN"),
    (0x80, "LEFT"), (0x100, "L2"), (0x200, "R2"), (0x400, "L1"), (0x800, "R1"), (0x1000, "TRIANGLE"),
    (0x2000, "CIRCLE"), (0x4000, "CROSS"), (0x8000, "SQUARE"), (0x100000, "TOUCHPAD"),
)
PORT_TYPES = {0: "main", 1: "bgm", 2: "voice", 3: "personal", 4: "padspk", 126: "audio3d", 127: "aux"}


def button_names(mask: int) -> str:
    names = [name for bit, name in BUTTONS if mask & bit]
    return "+".join(names) if names else "-"


def level_db(value: int) -> float:
    """Nivel del protocolo (x10000 de fondo de escala) en dBFS."""
    return 20.0 * math.log10(value / 10000.0) if value > 0 else -math.inf


def fmt_db(value: int) -> str:
    db = level_db(value)
    return "  -inf" if db == -math.inf else f"{db:6.1f}"


def quat_unrotate(q: tuple[float, float, float, float], v: tuple[float, float, float]) -> tuple[float, float, float]:
    """v en el marco local de la rotación q = (x, y, z, w): conj(q) * v * q."""
    x, y, z, w = q
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if norm < 1e-6:
        return v
    x, y, z, w = -x / norm, -y / norm, -z / norm, w / norm      # conjugado normalizado
    vx, vy, vz = v
    # t = 2 * cross(q.xyz, v); v' = v + w * t + cross(q.xyz, t)
    tx, ty, tz = 2 * (y * vz - z * vy), 2 * (z * vx - x * vz), 2 * (x * vy - y * vx)
    return (vx + w * tx + (y * tz - z * ty), vy + w * ty + (z * tx - x * tz), vz + w * tz + (x * ty - y * tx))


def damage_azimuth(ev: dict) -> float | None:
    """Ángulo horizontal de la fuente del daño en el marco del jugador, en grados (0 = delante,
    + = derecha), con la convención de Naughty Dog y arriba, z adelante [HIPÓTESIS: falta
    confirmar en el juego el signo y que la fuente +0x10 sea su posición]. None sin fuente."""
    if not ev.get("src") or not ev.get("loc"):
        return None
    d = ((ev.get("sx", 0) - ev.get("px", 0)) / 1000.0, (ev.get("sy", 0) - ev.get("py", 0)) / 1000.0,
         (ev.get("sz", 0) - ev.get("pz", 0)) / 1000.0)
    if abs(d[0]) + abs(d[2]) < 1e-3:
        return None
    q = (ev.get("qx", 0) / 10000.0, ev.get("qy", 0) / 10000.0, ev.get("qz", 0) / 10000.0, ev.get("qw", 10000) / 10000.0)
    lx, _, lz = quat_unrotate(q, d)
    return math.degrees(math.atan2(-lx, lz))


@dataclass
class Emitter:
    pid: int
    first_rx: float
    exe: str = "?"
    bin: str = "?"
    base: str = "?"
    version: str = "?"
    rumble_on: int | None = None
    min_seq: int | None = None
    max_seq: int | None = None
    received: int = 0
    last_rx: float = 0.0
    ended: str | None = None          # razón del bye
    silent_reported: bool = False
    types: Counter = field(default_factory=Counter)
    ports: dict = field(default_factory=dict)
    last_pad_buttons: int | None = None
    last_hello: dict | None = None
    last_gs: dict | None = None        # último estado del juego (Día 5) mostrado
    player_idle: bool | None = None    # refresco gs con ups=0 (el juego no actualiza al jugador)
    source: tuple[str, int] | None = None   # (ip, puerto) de sus datagramas: destino del canal fx (Día 8)

    @property
    def label(self) -> str:
        return f"{self.exe}#{self.pid}"

    def note_seq(self, seq: int) -> None:
        self.min_seq = seq if self.min_seq is None else min(self.min_seq, seq)
        self.max_seq = seq if self.max_seq is None else max(self.max_seq, seq)

    @property
    def lost(self) -> int:
        if self.min_seq is None or self.max_seq is None:
            return 0
        return max(0, (self.max_seq - self.min_seq + 1) - self.received)

    @property
    def expected(self) -> int:
        return 0 if self.min_seq is None or self.max_seq is None else self.max_seq - self.min_seq + 1


class Listener:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.start = time.perf_counter()
        self.emitters: dict[int, Emitter] = {}
        self.order: list[int] = []
        self.by_source: dict[str, Emitter] = {}   # cada proceso envía desde su propio puerto UDP
        self.unattributed = 0
        self.invalid = 0
        self.wrong_version = 0
        self.restarts = 0
        self.window_types: Counter = Counter()
        self.window_start = time.perf_counter()
        self.latencies: deque[int] = deque(maxlen=5000)
        self.show = self.parse_show(args.show)
        self.record = None
        self.record_path: Path | None = None
        self.lines_since_flush = 0
        self.last_flush = time.perf_counter()
        self.stop = False

    @staticmethod
    def parse_show(text: str) -> set[str]:
        items = {t.strip().lower() for t in text.split(",") if t.strip()}
        if "all" in items:
            return set(EVENT_TYPES) | {"pad-all"}
        if "none" in items:
            return set()
        unknown = items - set(EVENT_TYPES) - {"pad-all"}
        if unknown:
            raise SystemExit(f"--show: tipos desconocidos {sorted(unknown)}; válidos: {', '.join(EVENT_TYPES)}, pad-all, all, none")
        if "pad-all" in items:
            items.add("pad")
        return items

    # ---- salida -------------------------------------------------------------------------------
    def elapsed(self) -> float:
        return time.perf_counter() - self.start

    _print_lock = threading.Lock()   # varios hilos (gatillos, hápticos) imprimen a la vez

    def say(self, text: str) -> None:
        with self._print_lock:
            print(f"[{self.elapsed():9.3f}s] {text}", flush=True)

    def open_record(self) -> None:
        if self.args.no_record:
            return
        folder = Path(self.args.out)
        folder.mkdir(parents=True, exist_ok=True)
        name = time.strftime("unds_%Y%m%d_%H%M%S.jsonl")
        self.record_path = folder / name
        self.record = open(self.record_path, "w", encoding="utf-8", newline="\n")
        self.say(f"grabando en {self.record_path.resolve()}")

    def write(self, item: dict) -> None:
        if not self.record:
            return
        self.record.write(json.dumps(item, separators=(",", ":"), ensure_ascii=False) + "\n")
        self.lines_since_flush += 1
        now = time.perf_counter()
        if now - self.last_flush >= 1.0:
            self.record.flush()
            self.last_flush = now
            self.lines_since_flush = 0

    # ---- eventos ------------------------------------------------------------------------------
    def emitter_for(self, ev: dict, source: str, now: float) -> Emitter | None:
        pid = ev.get("pid")
        if ev.get("e") == "hello" and isinstance(pid, int):
            if pid not in self.emitters:
                if self.emitters:
                    self.restarts += 1
                self.emitters[pid] = Emitter(pid=pid, first_rx=now)
                self.order.append(pid)
            self.by_source[source] = self.emitters[pid]
            host, _, port = source.rpartition(":")
            self.emitters[pid].source = (host, int(port)) if port.isdigit() else None
            return self.emitters[pid]
        # El resto de eventos no lleva pid: se atribuyen por el puerto de origen del datagrama.
        return self.by_source.get(source)

    def handle(self, data: bytes, source: tuple[str, int]) -> None:
        now = time.perf_counter()
        rx_us = time.perf_counter_ns() // 1000
        item = {"rx_us": rx_us, "wall": round(time.time(), 6), "src": f"{source[0]}:{source[1]}"}
        try:
            ev = json.loads(data.decode("utf-8"))
            if not isinstance(ev, dict):
                raise ValueError("no es un objeto")
        except (UnicodeDecodeError, ValueError) as error:
            self.invalid += 1
            item["raw"] = data.decode("utf-8", "replace")
            self.write(item)
            if self.invalid <= 5:
                self.say(f"datagrama no válido ({error}): {item['raw'][:120]!r}")
            return
        item["ev"] = ev
        self.write(item)
        if ev.get("v") != PROTOCOL:
            self.wrong_version += 1
            if self.wrong_version <= 3:
                self.say(f"versión de protocolo {ev.get('v')!r}, se esperaba {PROTOCOL}: {ev}")
            return
        kind = ev.get("e", "?")
        self.window_types[kind] += 1
        t = ev.get("t")
        if isinstance(t, int):
            self.latencies.append(rx_us - t)

        emitter = self.emitter_for(ev, item["src"], now)
        if emitter is None:
            # Llegó antes del primer hello (el listener arrancó después del juego): el latido
            # de 1 s lo identifica enseguida; hasta entonces solo se graba.
            self.unattributed += 1
            return
        seq = ev.get("seq")
        if isinstance(seq, int):
            emitter.note_seq(seq)
        emitter.received += 1
        emitter.last_rx = now
        emitter.types[kind] += 1
        if emitter.silent_reported:
            emitter.silent_reported = False
            self.say(f"{emitter.label} vuelve a emitir")
        getattr(self, f"on_{kind}", self.on_unknown)(emitter, ev)

    def on_unknown(self, emitter: Emitter, ev: dict) -> None:
        if emitter.types[ev.get("e", "?")] <= 3:
            self.say(f"{emitter.label} evento desconocido: {ev}")

    def on_hello(self, em: Emitter, ev: dict) -> None:
        first = em.last_hello is None
        em.exe, em.bin, em.base = ev.get("exe", "?"), ev.get("bin", "?"), ev.get("base", "?")
        em.version, em.rumble_on = ev.get("ver", "?"), ev.get("rumble")
        em.last_hello = ev
        for spec in filter(None, str(ev.get("ports", "")).split(",")):
            try:
                port, kind, channels, frames = (int(x) for x in spec.split(":"))
                em.ports.setdefault(port, (kind, channels, frames))
            except ValueError:
                pass
        if first and "hello" in self.show:
            how = "arranque" if ev.get("boot") == 1 else "latido (listener iniciado tarde)"
            self.say(f"HELLO {em.label} [{how}] bin={em.bin} base={em.base} ver={em.version} "
                     f"rumble_sdl={'sí' if em.rumble_on else 'no'} puertos={ev.get('ports') or '-'}")
        if ev.get("err") or ev.get("drop"):
            if "hello" in self.show:
                self.say(f"{em.label} el emisor reporta drop={ev.get('drop')} err={ev.get('err')} "
                         f"(sendto rechazados)")
        if first and "hello" in self.show:
            if "gsp" in ev:
                self.say(f"{em.label} estado del juego: hooks activos (UNDS_STATE); se confirman con el primer "
                         f"STATE (jugador) — si no llega ninguno, esta versión del juego no coincide con las firmas")
            elif em.version >= "0.4.0":
                self.say(f"{em.label} estado del juego apagado (UNDS_STATE=0)")
        if ev.get("smax", 0) >= 1000 and "hello" in self.show:
            self.say(f"{em.label} AVISO: un sendto tardó {ev['smax']} us en el último segundo")

    def on_bye(self, em: Emitter, ev: dict) -> None:
        em.ended = str(ev.get("reason", "?"))
        if "bye" in self.show:
            self.say(f"BYE   {em.label} reason={em.ended} up={ev.get('up', 0) / 1000:.1f}s "
                     f"enviados={ev.get('sent')} recibidos={em.received} perdidos={em.lost}")

    def on_rumble(self, em: Emitter, ev: dict) -> None:
        if "rumble" in self.show and not ev.get("re"):
            l, s = ev.get("l", 0), ev.get("s", 0)
            bar = "#" * (max(l, s) * 20 // 255)
            self.say(f"RUMBLE {em.label} L={l:3d} S={s:3d} {bar}")

    def on_led(self, em: Emitter, ev: dict) -> None:
        if "led" in self.show and not ev.get("re"):
            self.say(f"LED   {em.label} rgb=({ev.get('r')},{ev.get('g')},{ev.get('b')})")

    def on_fx(self, em: Emitter, ev: dict) -> None:
        """Respuesta del canal fx del port (Día 8): una por orden de gatillos/lightbar."""
        if "fx" in self.show:
            self.say(f"FX    {em.label} c={ev.get('c')} f={ev.get('f')} ok={ev.get('ok')} q={ev.get('q')}us "
                     f"{ev.get('why')}")

    def on_pad(self, em: Emitter, ev: dict) -> None:
        buttons = ev.get("b", 0)
        changed = buttons != em.last_pad_buttons
        em.last_pad_buttons = buttons
        if "pad-all" in self.show or ("pad" in self.show and changed):
            self.say(f"PAD   {em.label} {button_names(buttons):<22} l2={ev.get('l2'):3d} r2={ev.get('r2'):3d} "
                     f"ls=({ev.get('lx')},{ev.get('ly')}) rs=({ev.get('rx')},{ev.get('ry')})")

    def on_aopen(self, em: Emitter, ev: dict) -> None:
        port = ev.get("p")
        em.ports[port] = (ev.get("ty"), ev.get("ch"), ev.get("fr"))
        if "aopen" in self.show or "audio" in self.show or "hello" in self.show:
            self.say(f"AOPEN {em.label} puerto {port} tipo {ev.get('ty')} ({PORT_TYPES.get(ev.get('ty'), '?')}) "
                     f"{ev.get('ch')} ch, {ev.get('fr')} frames")

    def on_audio(self, em: Emitter, ev: dict) -> None:
        if "audio" in self.show:
            self.say(f"AUDIO {em.label} p{ev.get('p'):<2} {PORT_TYPES.get(ev.get('ty'), '?'):<6} f={ev.get('f'):>10} "
                     f"rms={fmt_db(ev.get('rms', 0))} pk={fmt_db(ev.get('pk', 0))} lf={fmt_db(ev.get('lf', 0))} dBFS")

    # ---- estado del juego (Día 5, parche 0004) ---------------------------------------------------
    @staticmethod
    def weapon_text(ev: dict) -> str:
        w = ev.get("w", "0x0")
        if w in ("0x0", None) or ev.get("ws", -1) < 0:
            return "sin arma"
        return f"arma {w} [{ev.get('ws')}] {ev.get('clip')}/{ev.get('cap')} +{ev.get('res')}"

    def on_gs(self, em: Emitter, ev: dict) -> None:
        """Vida, arma y munición. Se imprime cuando cambia el arma, la munición, el estado de herido o
        la vida en >= 5 % (la regeneración manda pasos pequeños cada ~100 ms)."""
        last = em.last_gs
        em.last_gs = dict(ev) if not ev.get("re") else (last or dict(ev))
        if "gs" not in self.show:
            return
        if ev.get("re"):
            idle = ev.get("ups") == 0
            if idle != em.player_idle:
                if idle:
                    self.say(f"STATE {em.label} el juego no actualiza al jugador (ups=0: ¿cinemática, menú, carga?)")
                elif em.player_idle is not None:
                    self.say(f"STATE {em.label} jugador activo otra vez (ups={ev.get('ups')} en 0,5 s)")
                em.player_idle = idle
            return
        keys = ("ws", "w", "clip", "res", "cap", "inj", "gr", "grr")
        hp, hp_last = ev.get("hp", -1), (last or {}).get("hp", -1)
        if last is not None and all(ev.get(k) == last.get(k) for k in keys) and abs(hp - hp_last) < 50 \
                and hp not in (0, 1000) and hp_last != -1:
            em.last_gs = dict(last, hp=hp_last)              # se acumula hasta un cambio visible
            return
        hp_text = "vida ?" if hp < 0 else f"vida {hp / 10:5.1f}%"
        grenades = f" granadas {ev.get('gr')}/{ev.get('grr')}" if ev.get("gr", -1) >= 0 else ""
        self.say(f"STATE {em.label} {hp_text}{' HERIDO' if ev.get('inj') else ''} | {self.weapon_text(ev)}{grenades}")

    def on_shot(self, em: Emitter, ev: dict) -> None:
        if "shot" in self.show:
            self.say(f"SHOT  {em.label} arma {ev.get('w')} cargador {ev.get('clip')}->{ev.get('clip', 0) - 1}"
                     f"/{ev.get('cap')} reserva {ev.get('res')}")

    def on_dmg(self, em: Emitter, ev: dict) -> None:
        if "dmg" in self.show:
            where = ""
            angle = damage_azimuth(ev)
            if angle is not None:
                where = f" desde {angle:+5.0f}° (0 = delante, + = derecha) [HIPÓTESIS]"
            amount = "letal" if ev.get("amt", 0) >= 1000 else f"-{ev.get('amt', 0) / 10:.1f}%"
            self.say(f"DMG   {em.label} tipo {ev.get('ty')} {amount} "
                     f"(vida antes {ev.get('hp', 0) / 10:.1f}%){where}")

    # ---- estadísticas -------------------------------------------------------------------------
    def latency_text(self) -> str:
        if len(self.latencies) < 20:
            return "lat n/d"
        values = sorted(self.latencies)
        p50 = values[len(values) // 2]
        p99 = values[min(len(values) - 1, int(len(values) * 0.99))]
        if p50 < -1000 or p50 > 1_000_000:
            return "lat n/d (relojes con otra base)"
        return f"lat p50 {p50 / 1000:.2f} ms p99 {p99 / 1000:.2f} ms"

    def periodic(self) -> None:
        now = time.perf_counter()
        for em in self.emitters.values():
            if not em.ended and not em.silent_reported and em.last_rx and now - em.last_rx > self.args.timeout:
                em.silent_reported = True
                self.say(f"{em.label} sin paquetes desde hace {now - em.last_rx:.1f} s (¿cerrado sin bye, "
                         f"colgado o UNDS_EVENTS apunta a otro puerto?)")
        span = now - self.window_start
        if span < self.args.stats_every:
            return
        if self.args.stats_every > 0 and (self.window_types or self.emitters):
            rates = " ".join(f"{k} {self.window_types[k] / span:.0f}" for k in EVENT_TYPES if self.window_types[k])
            lost = sum(em.lost for em in self.emitters.values())
            expected = sum(em.expected for em in self.emitters.values())
            alive = [em.label for em in self.emitters.values() if not em.ended and not em.silent_reported]
            pct = 100.0 * lost / expected if expected else 0.0
            self.say(f"STATS {rates or 'sin eventos'} /s | perdidos {lost} ({pct:.3f}%) | {self.latency_text()} | "
                     f"vivos: {', '.join(alive) or '-'}")
        self.window_types.clear()
        self.window_start = now

    def summary(self) -> None:
        print()
        print("=" * 78)
        print(f"Resumen ({self.elapsed():.1f} s)")
        if self.record_path:
            print(f"  grabación: {self.record_path.resolve()}")
        print(f"  emisores: {len(self.emitters)}  reinicios del emisor (hello nuevo): {self.restarts}  "
              f"datagramas no válidos: {self.invalid}  versión distinta: {self.wrong_version}  "
              f"antes del primer hello: {self.unattributed}")
        print(f"  {self.latency_text()}")
        for pid in self.order:
            em = self.emitters[pid]
            pct = 100.0 * em.lost / em.expected if em.expected else 0.0
            print(f"  - {em.label}: bin={em.bin} base={em.base} recibidos={em.received} perdidos={em.lost} "
                  f"({pct:.3f}%) fin={em.ended or ('silencio' if em.silent_reported else 'activo')}")
            print(f"      por tipo: " + ", ".join(f"{k}={em.types[k]}" for k in EVENT_TYPES if em.types[k]))
            if em.ports:
                ports = ", ".join(f"{p}:{PORT_TYPES.get(v[0], v[0])}/{v[1]}ch/{v[2]}" for p, v in sorted(em.ports.items()))
                print(f"      puertos de audio: {ports}")
        print("=" * 78)

    # ---- bucle --------------------------------------------------------------------------------
    def run(self) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        except OSError:
            pass
        try:
            sock.bind((self.args.host, self.args.port))
        except OSError as error:
            print(f"No puedo escuchar en {self.args.host}:{self.args.port}: {error}\n"
                  f"¿Otro unds_listen.py (o DSX) ya usa ese puerto? Cambia --port y UNDS_EVENTS a la vez.",
                  file=sys.stderr)
            return 2
        sock.settimeout(0.2)
        rcvbuf = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        self.say(f"escuchando UDP {self.args.host}:{self.args.port} (protocolo v{PROTOCOL}, rcvbuf {rcvbuf} B). "
                 f"En el launcher: UNDS_EVENTS={self.args.host}:{self.args.port}")
        self.open_record()
        try:
            while not self.stop:
                try:
                    data, source = sock.recvfrom(65535)
                except socket.timeout:
                    data = None
                except ConnectionResetError:
                    # Windows: un ICMP "puerto inalcanzable" previo; en UDP no significa nada aquí.
                    data = None
                if data is not None:
                    self.handle(data, source)
                self.periodic()
        except KeyboardInterrupt:
            pass
        finally:
            sock.close()
            if self.record:
                self.record.flush()
                self.record.close()
            self.summary()
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Escucha y graba el bus de eventos UNDS (UDP, JSON v1).")
    parser.add_argument("--host", default="127.0.0.1", help="dirección donde escuchar (127.0.0.1)")
    parser.add_argument("--port", type=int, default=47111, help="puerto UDP (47111)")
    parser.add_argument("--out", default="recordings", help="carpeta de grabaciones (recordings)")
    parser.add_argument("--no-record", action="store_true", help="no grabar JSONL")
    parser.add_argument("--show", default="hello,bye,rumble,led,pad,aopen,gs,shot,dmg",
                        help="qué imprimir en vivo: hello,bye,rumble,led,pad,aopen,audio,gs,shot,dmg,pad-all | all | none. "
                             "'pad' solo imprime cambios de botones; 'pad-all' cada evento pad")
    parser.add_argument("--stats-every", type=float, default=2.0, help="segundos entre líneas STATS (0 = nunca)")
    parser.add_argument("--timeout", type=float, default=3.0, help="segundos sin paquetes para avisar de un emisor callado")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port fuera de rango")
    if os.name == "nt":
        # Consola de Windows en UTF-8 para los acentos y la barra de rumble.
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    listener = Listener(args)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    return listener.run()


if __name__ == "__main__":
    sys.exit(main())
