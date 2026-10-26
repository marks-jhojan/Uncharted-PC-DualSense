#!/usr/bin/env python3
"""UNDS — cliente UDP del "Mod System" de DSX (v3.1+, sistema heredado de v2) y prueba guiada.

Protocolo (fuente: github.com/Paliverse/DSX, carpeta "Mod System (DSX v3)": README.md y
Resources.cs del ejemplo oficial en C#, commit de 2025-06-23):
  - UDP a 127.0.0.1:<puerto>. Puerto en %LOCALAPPDATA%\\DSX\\DSX_UDP_PortNumber.txt (DSX v3.1
    BETA 1.37 o superior); si no existe, 6969.
  - Datagrama ASCII con un JSON (Newtonsoft, enums como enteros):
        {"instructions":[{"type":<InstructionType>,"parameters":[...]} , ...]}
  - InstructionType: GetDSXStatus=0 TriggerUpdate=1 RGBUpdate=2 PlayerLED=3 TriggerThreshold=4
                     MicLED=5 PlayerLEDNewRevision=6 ResetToUserSettings=7
  - El primer parámetro es el índice del mando (Devices[].Index de la respuesta de estado).
  - TriggerUpdate: [index, Trigger(Left=1, Right=2), TriggerMode, ...parámetros del modo]
  - RGBUpdate: [index, r, g, b, brillo]  (0-255 cada uno)
  - DSX responde al puerto de origen con un JSON: Status, TimeReceived, isControllerConnected,
    BatteryLevel, Devices[{Index, MacAddress, DeviceType, ConnectionType, BatteryLevel,
    IsSupportAT, IsSupportLightBar, IsSupportPlayerLED, IsSupportMicLED}].
[HIPÓTESIS] si DSX responde a cada paquete o solo a GetDSXStatus, si los enums de la respuesta
llegan como números o como texto y cuánto tarda DSX en llevar un cambio al mando: lo mide
`python unds_dsx.py probe`.

Uso (CMD, en la carpeta companion):
    python unds_dsx.py status                     estado de DSX y mandos conectados
    python unds_dsx.py probe                      prueba guiada (gatillos, LED, cambios rápidos, RTT)
    python unds_dsx.py set R "WEAPON 2 4 6"       aplica un efecto a R2 (L para L2)
    python unds_dsx.py rgb 255 0 0 [brillo]       lightbar
    python unds_dsx.py reset                      vuelve al perfil de usuario de DSX
    python unds_dsx.py rtt [n]                    latencia de ida y vuelta con DSX (n=100)

Solo biblioteca estándar (Python 3.10+).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---- protocolo ---------------------------------------------------------------------------------
GET_STATUS, TRIGGER_UPDATE, RGB_UPDATE, PLAYER_LED, TRIGGER_THRESHOLD, MIC_LED, PLAYER_LED_NEW, RESET = range(8)
INSTRUCTION_NAMES = {0: "GetDSXStatus", 1: "TriggerUpdate", 2: "RGBUpdate", 3: "PlayerLED",
                     4: "TriggerThreshold", 5: "MicLED", 6: "PlayerLEDNewRevision", 7: "ResetToUserSettings"}
LEFT, RIGHT = 1, 2

# Modos v3 (Resources.cs) con los rangos de parámetros del README oficial.
V3_MODES: dict[str, tuple[int, tuple[tuple[int, int], ...]]] = {
    "OFF": (20, ()),
    "FEEDBACK": (21, ((1, 9), (1, 8))),                              # inicio, fuerza
    "WEAPON": (22, ((2, 7), (3, 8), (1, 8))),                        # inicio, fin, fuerza
    "VIBRATION": (23, ((1, 9), (1, 8), (1, 40))),                    # inicio, amplitud, frecuencia
    "SLOPE_FEEDBACK": (24, ((1, 8), (2, 9), (1, 8), (1, 8))),        # inicio, fin, fuerza ini, fuerza fin
    "MULTIPLE_POSITION_FEEDBACK": (25, ((0, 8),) * 10),              # fuerza por zona (10 zonas)
    "MULTIPLE_POSITION_VIBRATION": (26, ((1, 40),) + ((0, 8),) * 10),  # frecuencia + amplitud por zona
    # Solo backend HID (DSX v3 no lo tiene; su MACHINE heredado no lleva parámetros). Efecto 0x27 del
    # mando, "no oficial" según la factoría de Nielk1 (MIT): oscila entre dos fuerzas.
    "MACHINE": (90, ((1, 8), (2, 9), (0, 7), (0, 7), (1, 255), (1, 255))),  # inicio, fin, fuerza A, fuerza B, Hz, periodo (0,1 s)
}
HID_ONLY_MODES = frozenset({90})
# Modos heredados de v2 (el README pide evitarlos): se envían sin validar parámetros.
LEGACY_MODES = {
    "NORMAL": 0, "GAMECUBE": 1, "VERYSOFT": 2, "SOFT": 3, "HARD": 4, "VERYHARD": 5, "HARDEST": 6,
    "RIGID": 7, "VIBRATETRIGGER": 8, "CHOPPY": 9, "MEDIUM": 10, "VIBRATETRIGGERPULSE": 11,
    "CUSTOMTRIGGERVALUE": 12, "RESISTANCE": 13, "BOW": 14, "GALLOPING": 15, "SEMIAUTOMATICGUN": 16,
    "AUTOMATICGUN": 17, "MACHINE": 18, "VIBRATE_TRIGGER_10HZ": 19,
}
ALIASES = {"SLOPE": "SLOPE_FEEDBACK", "MPF": "MULTIPLE_POSITION_FEEDBACK", "MPV": "MULTIPLE_POSITION_VIBRATION",
           "VIB": "VIBRATION"}
DEFAULT_PORT = 6969


@dataclass(frozen=True)
class Effect:
    """Efecto de gatillo: modo DSX y parámetros ya validados. `text` es la forma canónica."""
    mode: int
    params: tuple[int, ...]
    text: str

    def __str__(self) -> str:
        return self.text


def parse_effect(text: str) -> Effect:
    """'WEAPON 2 4 6', 'feedback 3 2', 'OFF', 'vib 1 8 30' -> Effect. ValueError si no es válido."""
    parts = text.replace(",", " ").split()
    if not parts:
        raise ValueError("efecto vacío")
    name = ALIASES.get(parts[0].upper(), parts[0].upper())
    try:
        values = tuple(int(p) for p in parts[1:])
    except ValueError:
        raise ValueError(f"{text!r}: los parámetros deben ser enteros") from None
    if name in V3_MODES:
        mode, ranges = V3_MODES[name]
        if len(values) != len(ranges):
            raise ValueError(f"{name} necesita {len(ranges)} parámetros, hay {len(values)}")
        for i, (v, (lo, hi)) in enumerate(zip(values, ranges), 1):
            if not lo <= v <= hi:
                raise ValueError(f"{name}: parámetro {i} = {v} fuera de {lo}-{hi}")
        if name == "WEAPON" and values[1] <= values[0]:
            raise ValueError("WEAPON: el fin debe ser mayor que el inicio")
        if name == "SLOPE_FEEDBACK" and values[1] <= values[0]:
            raise ValueError("SLOPE_FEEDBACK: el fin debe ser mayor que el inicio")
        if name == "MACHINE" and values[1] <= values[0]:
            raise ValueError("MACHINE: el fin debe ser mayor que el inicio")
    elif name in LEGACY_MODES:
        mode = LEGACY_MODES[name]
    else:
        raise ValueError(f"modo desconocido {parts[0]!r}; v3: {', '.join(V3_MODES)}")
    return Effect(mode, values, " ".join([name, *map(str, values)]))


def find_port() -> tuple[int, str]:
    """(puerto, de dónde salió)."""
    local = os.environ.get("LOCALAPPDATA")
    candidates = []
    if local:
        candidates.append(Path(local) / "DSX" / "DSX_UDP_PortNumber.txt")
    candidates.append(Path(r"C:\Temp\DualSenseX\DualSenseX_PortNumber.txt"))  # DSX v2
    for path in candidates:
        try:
            value = int(path.read_text(encoding="ascii", errors="replace").strip())
            if 1 <= value <= 65535:
                return value, str(path)
        except (OSError, ValueError):
            continue
    return DEFAULT_PORT, "por defecto (no hay archivo de puerto)"


@dataclass
class Response:
    rx: float
    data: dict | None
    raw: str
    after: str  # tipo del último paquete enviado antes de recibirla
    rtt_ms: float | None


@dataclass
class DSXClient:
    """Socket UDP propio (puerto efímero) para que DSX pueda responder. Un hilo lee respuestas."""
    port: int | None = None
    host: str = "127.0.0.1"
    port_source: str = ""
    sent: int = 0
    send_errors: int = 0
    responses: list[Response] = field(default_factory=list)   # las últimas `keep`
    keep: int = 200
    total: int = 0                                             # respuestas recibidas desde el inicio
    by_kind: dict = field(default_factory=dict)                # respuestas por tipo de paquete previo

    def __post_init__(self) -> None:
        if self.port is None:
            self.port, self.port_source = find_port()
        else:
            self.port_source = "argumento"
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.host, 0))
        self.sock.settimeout(0.2)
        self.lock = threading.Lock()
        self.got = threading.Condition(self.lock)
        self.last_send = 0.0
        self.last_kind = "-"
        self.reset_errors = 0
        self.closed = False
        self.reader = threading.Thread(target=self._read, name="dsx-rx", daemon=True)
        self.reader.start()

    # -- bajo nivel --
    def _read(self) -> None:
        while not self.closed:
            try:
                data, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except ConnectionResetError:
                # Windows: ICMP "puerto inalcanzable" -> nadie escucha en el puerto de DSX.
                with self.lock:
                    self.reset_errors += 1
                continue
            except OSError:
                if self.closed:
                    return
                continue
            now = time.perf_counter()
            text = data.decode("ascii", "replace")
            try:
                parsed = json.loads(text)
                if not isinstance(parsed, dict):
                    parsed = None
            except ValueError:
                parsed = None
            with self.lock:
                rtt = (now - self.last_send) * 1000.0 if self.last_send else None
                self.responses.append(Response(now, parsed, text, self.last_kind, rtt))
                del self.responses[:-self.keep]
                self.total += 1
                self.by_kind[self.last_kind] = self.by_kind.get(self.last_kind, 0) + 1
                self.got.notify_all()

    def send(self, instructions: list[tuple[int, list[int]]]) -> bool:
        packet = {"instructions": [{"type": t, "parameters": p} for t, p in instructions]}
        payload = json.dumps(packet, separators=(",", ":")).encode("ascii")
        kinds = "+".join(sorted({INSTRUCTION_NAMES.get(t, str(t)) for t, _ in instructions}))
        with self.lock:
            # antes del sendto: DSX puede responder (~0,1 ms) antes de que esta función siga
            self.last_send = time.perf_counter()
            self.last_kind = kinds
        try:
            self.sock.sendto(payload, (self.host, self.port))
        except OSError:
            with self.lock:
                self.send_errors += 1
            return False
        with self.lock:
            self.sent += 1
        return True

    def wait_response(self, since: int, timeout: float) -> Response | None:
        """Primera respuesta con número >= since (response_count() antes de enviar)."""
        end = time.perf_counter() + timeout
        with self.lock:
            while self.total <= since:
                left = end - time.perf_counter()
                if left <= 0:
                    return None
                self.got.wait(left)
            first_kept = self.total - len(self.responses)
            return self.responses[max(0, since - first_kept)]

    def response_count(self) -> int:
        with self.lock:
            return self.total

    def close(self) -> None:
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass

    # -- alto nivel --
    def status(self, timeout: float = 1.0) -> Response | None:
        since = self.response_count()
        self.send([(GET_STATUS, [])])
        return self.wait_response(since, timeout)

    @staticmethod
    def trigger(index: int, side: int, effect: Effect) -> tuple[int, list[int]]:
        return TRIGGER_UPDATE, [index, side, effect.mode, *effect.params]

    @staticmethod
    def rgb(index: int, r: int, g: int, b: int, brightness: int = 255) -> tuple[int, list[int]]:
        clamp = lambda v: max(0, min(255, int(v)))  # noqa: E731
        return RGB_UPDATE, [index, clamp(r), clamp(g), clamp(b), clamp(brightness)]

    @staticmethod
    def reset(index: int) -> tuple[int, list[int]]:
        return RESET, [index]

    @staticmethod
    def player_led(index: int, value: int) -> tuple[int, list[int]]:
        return PLAYER_LED_NEW, [index, value]  # One=0 .. Five=4, AllOff=5


def devices_of(resp: Response | None) -> list[dict]:
    if not resp or not resp.data:
        return []
    devices = resp.data.get("Devices")
    return devices if isinstance(devices, list) else []


def pick_controller(resp: Response | None) -> tuple[int | None, str]:
    """Primer DualSense (DeviceType 0/"DUALSENSE" o 1/"DUALSENSE_EDGE") con gatillos adaptativos."""
    devices = devices_of(resp)
    for d in devices:
        kind = d.get("DeviceType")
        if kind in (0, 1, "DUALSENSE", "DUALSENSE_EDGE") and d.get("IsSupportAT", True):
            return int(d.get("Index", 0)), describe_device(d)
    if devices:
        d = devices[0]
        return int(d.get("Index", 0)), describe_device(d) + " (no es DualSense)"
    return None, "sin mandos en la respuesta"


def describe_device(d: dict) -> str:
    conn = {0: "USB", 1: "BT", 2: "dongle"}.get(d.get("ConnectionType"), d.get("ConnectionType"))
    kind = {0: "DualSense", 1: "DualSense Edge", 2: "DS4 v1", 3: "DS4 v2"}.get(d.get("DeviceType"), d.get("DeviceType"))
    return (f"índice {d.get('Index')} {kind} por {conn}, batería {d.get('BatteryLevel')}%, "
            f"AT={d.get('IsSupportAT')} LED={d.get('IsSupportLightBar')} MAC {d.get('MacAddress')}")


# ---- CLI -----------------------------------------------------------------------------------------
def cmd_status(client: DSXClient, args: argparse.Namespace) -> int:
    print(f"DSX en {client.host}:{client.port} ({client.port_source})")
    resp = client.status(timeout=args.timeout)
    if resp is None:
        reset = client.reset_errors
        print("SIN RESPUESTA en", args.timeout, "s.",
              "Windows dice que nadie escucha en ese puerto (ICMP)." if reset else "",
              "\nComprueba: DSX abierto (v3.1+), mando conectado y que el puerto coincide con el de DSX.")
        return 1
    print(f"Respuesta en {resp.rtt_ms:.2f} ms:")
    print(json.dumps(resp.data, indent=2, ensure_ascii=False) if resp.data else resp.raw)
    index, desc = pick_controller(resp)
    print(f"\nMando elegido: {desc}" if index is not None else f"\n{desc}")
    return 0


def resolve_index(client: DSXClient, args: argparse.Namespace) -> int:
    if args.controller is not None:
        return args.controller
    index, _ = pick_controller(client.status(timeout=args.timeout))
    return 0 if index is None else index


def cmd_set(client: DSXClient, args: argparse.Namespace) -> int:
    effect = parse_effect(args.effect)
    side = LEFT if args.side.upper() in ("L", "L2", "LEFT") else RIGHT
    index = resolve_index(client, args)
    since = client.response_count()
    client.send([client.trigger(index, side, effect)])
    resp = client.wait_response(since, args.timeout)
    print(f"{'L2' if side == LEFT else 'R2'} <- {effect} (mando {index}); respuesta: "
          f"{'%.2f ms' % resp.rtt_ms if resp else 'ninguna'}")
    return 0


def cmd_rgb(client: DSXClient, args: argparse.Namespace) -> int:
    index = resolve_index(client, args)
    client.send([client.rgb(index, args.r, args.g, args.b, args.brightness)])
    print(f"RGB ({args.r},{args.g},{args.b}) brillo {args.brightness} -> mando {index}")
    return 0


def cmd_rtt(client: DSXClient, args: argparse.Namespace) -> int:
    """Solo la medida de ida y vuelta de GetDSXStatus (sin tocar gatillos)."""
    rtts = []
    for _ in range(args.count):
        r = client.status(timeout=0.5)
        if r and r.rtt_ms is not None:
            rtts.append(r.rtt_ms)
        time.sleep(0.01)
    if not rtts:
        print("sin respuestas")
        return 1
    rtts.sort()
    print(f"n={len(rtts)}/{args.count} p50={rtts[len(rtts) // 2]:.2f} ms "
          f"p99={rtts[max(0, int(len(rtts) * 0.99) - 1)]:.2f} ms max={rtts[-1]:.2f} ms")
    return 0


def cmd_reset(client: DSXClient, args: argparse.Namespace) -> int:
    index = resolve_index(client, args)
    client.send([client.reset(index)])
    print(f"ResetToUserSettings -> mando {index}")
    return 0


def ask(question: str) -> str:
    try:
        answer = input(f"  {question} [s/n/texto] > ").strip()
    except EOFError:
        answer = ""
    return answer


def cmd_probe(client: DSXClient, args: argparse.Namespace) -> int:
    """Prueba guiada. Guarda todo en recordings/dsx_probe_<fecha>.json para el análisis."""
    report: dict = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "port": client.port, "port_source": client.port_source,
                    "steps": []}
    print(f"== Paso 1: estado de DSX en {client.host}:{client.port} ({client.port_source})")
    resp = client.status(timeout=args.timeout)
    report["status"] = resp.data if resp and resp.data else (resp.raw if resp else None)
    report["status_rtt_ms"] = resp.rtt_ms if resp else None
    if resp is None:
        print("  SIN RESPUESTA. Abre DSX, conecta el mando y repite. (¿Puerto distinto? mira Ajustes de DSX.)")
        save_report(report, args)
        return 1
    print(f"  respuesta en {resp.rtt_ms:.2f} ms")
    index, desc = pick_controller(resp)
    if args.controller is not None:
        index = args.controller
    print(f"  mando: {desc}")
    if index is None:
        save_report(report, args)
        return 1
    report["controller"] = index
    try:
        return run_probe_steps(client, args, report, index)
    finally:
        client.send([client.reset(index)])  # también con Ctrl+C: no dejar el gatillo duro
        save_report(report, args)


def run_probe_steps(client: DSXClient, args: argparse.Namespace, report: dict, index: int) -> int:
    def step(name: str, instructions: list[tuple[int, list[int]]], question: str, hold: float = 0.0) -> None:
        since = client.response_count()
        client.send(instructions)
        r = client.wait_response(since, 0.5)
        print(f"\n== {name}")
        print(f"  enviado: {json.dumps([{'type': t, 'parameters': p} for t, p in instructions])}")
        print(f"  respuesta: {'%.2f ms' % r.rtt_ms if r else 'ninguna (¿DSX solo responde a GetDSXStatus?)'}")
        if hold:
            time.sleep(hold)
        answer = ask(question)
        report["steps"].append({"name": name, "sent": instructions, "response_ms": r.rtt_ms if r else None,
                                "response": (r.data or r.raw) if r else None, "answer": answer})

    tr = client.trigger
    step("R2 FEEDBACK 1 8 (pared desde el inicio)", [tr(index, RIGHT, parse_effect("FEEDBACK 1 8"))],
         "¿R2 duro desde el principio del recorrido?")
    step("R2 WEAPON 2 5 8 (resistencia que se rompe)", [tr(index, RIGHT, parse_effect("WEAPON 2 5 8"))],
         "¿R2 tiene un 'clic' que se rompe a media carrera? ¿Aprox. en qué % del recorrido?")
    step("R2 VIBRATION 1 8 30", [tr(index, RIGHT, parse_effect("VIBRATION 1 8 30"))],
         "¿R2 vibra al presionarlo?")
    step("L2 SLOPE_FEEDBACK 1 9 1 8 + R2 OFF en UN paquete",
         [tr(index, LEFT, parse_effect("SLOPE_FEEDBACK 1 9 1 8")), tr(index, RIGHT, parse_effect("OFF"))],
         "¿L2 se endurece progresivamente Y R2 quedó libre? (prueba los dos)")
    step("Lightbar roja", [client.rgb(index, 255, 0, 0, 255)], "¿Lightbar en rojo?")
    step("Lightbar amarillo tenue como U1 en juego (32,32,0)", [client.rgb(index, 32, 32, 0, 255)],
         "¿Se ve (tenue)? ¿Demasiado oscuro?")

    # Cambios rápidos: ¿DSX aplica cada cambio o los agrupa? (límite inferior útil del 'kick')
    for period_ms in (100, 50, 25):
        print(f"\n== Cambios rápidos: R2 alterna OFF / FEEDBACK 1 8 cada {period_ms} ms durante 3 s")
        print("  Mantén R2 apretado hasta la mitad y sigue apretado.")
        ask("¿Listo? (Enter)")
        hard, off = parse_effect("FEEDBACK 1 8"), parse_effect("OFF")
        t0 = time.perf_counter()
        n = 0
        while time.perf_counter() - t0 < 3.0:
            client.send([tr(index, RIGHT, hard if n % 2 == 0 else off)])
            n += 1
            target = t0 + n * period_ms / 1000.0
            while time.perf_counter() < target:
                time.sleep(0.001)
        client.send([tr(index, RIGHT, off)])
        answer = ask(f"¿Sentiste ~{1000 // (2 * period_ms)} golpes por segundo regulares, menos, o nada?")
        report["steps"].append({"name": f"toggle {period_ms} ms", "packets": n, "answer": answer})

    # Latencia de ida y vuelta con GetDSXStatus (solo mide DSX, no el mando).
    print("\n== RTT de GetDSXStatus (100 veces)")
    rtts = []
    for _ in range(100):
        r = client.status(timeout=0.5)
        if r and r.rtt_ms is not None:
            rtts.append(r.rtt_ms)
        time.sleep(0.01)
    if rtts:
        rtts.sort()
        report["rtt_ms"] = {"n": len(rtts), "p50": rtts[len(rtts) // 2], "p99": rtts[int(len(rtts) * 0.99) - 1],
                            "max": rtts[-1]}
        print(f"  n={len(rtts)} p50={report['rtt_ms']['p50']:.2f} ms p99={report['rtt_ms']['p99']:.2f} ms "
              f"max={rtts[-1]:.2f} ms")
    else:
        print("  sin respuestas")
    # ¿Responde a todo o solo a GetDSXStatus?
    with client.lock:
        report["responses_by_kind"] = dict(client.by_kind)
    print(f"  respuestas por tipo de paquete previo: {report['responses_by_kind']}")

    client.send([client.reset(index)])
    report["steps"].append({"name": "reset", "answer": ask("ResetToUserSettings enviado: ¿gatillos y LED volvieron a tu perfil de DSX?")})
    return 0


def save_report(report: dict, args: argparse.Namespace) -> None:
    folder = Path(args.out)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / time.strftime("dsx_probe_%Y%m%d_%H%M%S.json")
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nInforme guardado en {path.resolve()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cliente UDP de DSX v3.1+ (Mod System heredado de v2).")
    parser.add_argument("--port", type=int, default=None, help="puerto UDP de DSX (por defecto, el del archivo de DSX o 6969)")
    parser.add_argument("--controller", type=int, default=None, help="índice del mando (por defecto, el primer DualSense)")
    parser.add_argument("--timeout", type=float, default=1.0, help="espera de respuesta en s (1.0)")
    parser.add_argument("--out", default="recordings", help="carpeta del informe de probe (recordings)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("probe")
    p = sub.add_parser("set")
    p.add_argument("side", help="L o R")
    p.add_argument("effect", help='p. ej. "WEAPON 2 4 6", "FEEDBACK 3 2", "VIBRATION 1 8 30", "OFF"')
    p = sub.add_parser("rgb")
    for name in ("r", "g", "b"):
        p.add_argument(name, type=int)
    p.add_argument("brightness", type=int, nargs="?", default=255)
    sub.add_parser("reset")
    p = sub.add_parser("rtt")
    p.add_argument("count", type=int, nargs="?", default=100)
    args = parser.parse_args(argv)
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    client = DSXClient(port=args.port)
    try:
        return {"status": cmd_status, "probe": cmd_probe, "set": cmd_set, "rgb": cmd_rgb,
                "reset": cmd_reset, "rtt": cmd_rtt}[args.cmd](client, args)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
