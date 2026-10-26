#!/usr/bin/env python3
"""UNDS — gatillos adaptativos y lightbar del DualSense a través del PORT (canal fx, parche 0004).

Por qué (Día 8): con el backend hid el companion abría el mando con hidapi y escribía su propio
reporte de salida, al lado del SDL del port: dos escritores del mismo reporte, solo USB, y una
dependencia (hidapi) más. Ahora el companion manda cada cambio al port por UDP, a la dirección de
donde le llegan los eventos (la aprende del hello), y el port lo entrega a SDL_SendGamepadEffect:
SDL queda como único escritor, por USB o Bluetooth. Los efectos se codifican igual que en
unds_hid.py (los 11 bytes del mando), así que unds_triggers.ini no cambia.

Datagrama (37 bytes, little endian; runtime_unds.h):
    0 "UNDC"  4 versión 1  5 flags (1 R2, 2 L2, 4 lightbar, 8 soltar)  6-7 cero  8 seq u32
    12 efecto R2 (11 B)  23 efecto L2 (11 B)  34 r g b
El port contesta cada orden con un evento "fx" (ok 1 escrito, 0 aún sin mando, -1 no es un
DualSense, -2 SDL lo rechazó; q = µs dentro del port) y suelta los gatillos solo si deja de
recibir datagramas 5 s (el motor reenvía el estado cada reassert_s = 2 s), al "bye" o al soltar.

Si el port muere sin bye (cuelgue, crash) nadie puede soltar los gatillos por SDL: si hidapi está
instalado se sueltan por HID directo una vez (rescate); si no, se avisa.

    python unds_port.py --test [--port 47111]   prueba guiada con el juego abierto (espera un hello)
"""
from __future__ import annotations

import argparse
import socket
import struct
import sys
import threading
import time
from collections import Counter, deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from unds_dsx import LEFT, RGB_UPDATE, RESET, RIGHT, TRIGGER_UPDATE, Response, parse_effect  # noqa: E402
from unds_hid import RELEASE_RGB, dsx_running, encode_effect  # noqa: E402  (hidapi NO hace falta aquí)

MAGIC, VERSION, SIZE = b"UNDC", 1, 37
F_RIGHT, F_LEFT, F_LED, F_RELEASE = 0x01, 0x02, 0x04, 0x08
_PACK = struct.Struct("<4sBBHI11s11s3s")
assert _PACK.size == SIZE
EMPTY11 = bytes(11)
ACK_TEXT = {0: "el port aún no tiene mando abierto (menús de carga: se aplica en cuanto lo abra)",
            -1: "el mando que usa el port no es un DualSense: gatillos y lightbar ignorados",
            -2: "SDL rechazó el efecto (mira 'fx:' en user\\last_run.log del port)"}
RESCUE_AFTER_S = 3.0   # sin acks durante este tiempo al soltar = el port ya no está (crash): rescate por HID


def pack_command(seq: int, flags: int, right: bytes | None = None, left: bytes | None = None,
                 rgb: tuple[int, int, int] | None = None) -> bytes:
    """Datagrama fx v1. Las partes sin flag van a cero (el port no las mira)."""
    for name, data in (("R2", right), ("L2", left)):
        if data is not None and len(data) != 11:
            raise ValueError(f"efecto de {name} de {len(data)} bytes (deben ser 11)")
    colour = bytes(max(0, min(255, int(c))) for c in rgb) if rgb is not None else bytes(3)
    return _PACK.pack(MAGIC, VERSION, flags & 0xFF, 0, seq & 0xFFFFFFFF, right or EMPTY11, left or EMPTY11, colour)


def unpack_command(data: bytes) -> dict:
    """Inverso de pack_command (tests y depuración)."""
    if len(data) != SIZE:
        raise ValueError(f"{len(data)} bytes (deben ser {SIZE})")
    magic, version, flags, _, seq, right, left, rgb = _PACK.unpack(data)
    if magic != MAGIC or version != VERSION:
        raise ValueError("no es un datagrama fx v1")
    return {"flags": flags, "seq": seq, "right": right, "left": left, "rgb": tuple(rgb)}


class PortClient:
    """Interfaz de HidClient/DSXClient para el motor de gatillos (send/status/reset/close y contadores);
    el destino es el port que mandó el último hello (retarget)."""

    label = "PORT"

    def __init__(self, say=print, rescue: bool = True):
        self.say = say
        self.rescue_enabled = rescue
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.lock = threading.Lock()
        self.target: tuple[str, int] | None = None
        self.target_pid: int | None = None
        self.target_fx: bool | None = None       # el hello dice fx = 1
        self.port = "-"                           # para los mensajes del motor
        self.port_source = "canal fx del port"
        self.seq = 0
        self.sent = 0
        self.send_errors = 0
        self.reset_errors = 0
        self.acks = 0
        self.ack_results: Counter = Counter()
        self.ack_said: set[int] = set()
        self.pending: dict[int, float] = {}       # seq -> instante de envío (perf_counter)
        self.rtt_ms: deque[float] = deque(maxlen=4000)
        self.port_us: deque[int] = deque(maxlen=4000)
        self.last_ack = -1e9
        self.applied = False                      # se ha mandado algo distinto de "soltar" desde el último soltar
        self.waiting_said = False
        self.rescues = 0

    # -- destino --
    def retarget(self, source: tuple[str, int] | None, pid: int, hello: dict) -> None:
        """Nuevo proceso del port (hello de otro pid). source = (ip, puerto) del datagrama."""
        fx = hello.get("fx")
        with self.lock:
            self.target_pid = pid
            self.target_fx = fx == 1
            self.pending.clear()
            if not self.target_fx or source is None:
                self.target = None
            else:
                port = int(hello.get("fxp") or source[1])
                if port != source[1]:
                    self.say(f"PORT  aviso: el hello viene del puerto {source[1]} pero anuncia fxp={port}; uso {source[1]}")
                self.target = (source[0], source[1])
                self.port = f"{source[0]}:{source[1]}"
                self.waiting_said = False
        if hello.get("replay"):
            self.say("PORT  replay: sin port real no hay canal fx; para sentir los gatillos en un replay usa "
                     "--backend hid (con hidapi)")
        elif fx is None:
            self.say("PORT  el port NO trae el canal fx (parche 0004): compila con unds_build.sh o usa "
                     "[output] backend = hid. Hasta entonces no hay gatillos ni lightbar.")
        elif fx != 1:
            self.say("PORT  el port corre con UNDS_FX=0 (canal apagado): quítalo del launcher o usa backend = hid.")
        else:
            self.say(f"PORT  gatillos y lightbar por el port (SDL) en {self.port}")

    # -- envío --
    def _send(self, flags: int, right: bytes | None, left: bytes | None, rgb: tuple[int, int, int] | None) -> bool:
        with self.lock:
            target = self.target
            if target is None:
                self.send_errors += 1
                if not self.waiting_said:
                    self.waiting_said = True
                    self.say("PORT  sin destino todavía (espero el hello de un port con canal fx)")
                return False
            self.seq = (self.seq + 1) & 0xFFFFFFFF or 1
            seq = self.seq
            data = pack_command(seq, flags, right, left, rgb)
            try:
                self.sock.sendto(data, target)
            except OSError as error:
                self.send_errors += 1
                self.say(f"PORT  sendto falló ({error})")
                return False
            self.sent += 1
            self.pending[seq] = time.perf_counter()
            if len(self.pending) > 256:                    # sin acks (port viejo, replay): no crecer
                for old in sorted(self.pending)[:128]:
                    del self.pending[old]
            self.applied = not (flags & F_RELEASE)
            return True

    def send(self, instructions: list[tuple[int, list[int]]]) -> bool:
        right = left = rgb = None
        release = False
        for kind, params in instructions:
            if kind == TRIGGER_UPDATE:
                _, side, mode, *rest = params
                data = encode_effect(mode, tuple(rest))
                if side == RIGHT:
                    right = data
                elif side == LEFT:
                    left = data
            elif kind == RGB_UPDATE:
                _, r, g, b, brightness = params
                k = brightness / 255.0
                rgb = (round(r * k), round(g * k), round(b * k))
            elif kind == RESET:
                release = True
        if release:
            return self.release()
        flags = (F_RIGHT if right is not None else 0) | (F_LEFT if left is not None else 0) | (F_LED if rgb is not None else 0)
        if not flags:
            return True
        return self._send(flags, right, left, rgb)

    def keepalive(self) -> bool:
        return self._send(0, None, None, None)

    def release(self) -> bool:
        """Gatillos libres y lightbar al juego. Si el port no contesta desde hace RESCUE_AFTER_S y habíamos
        aplicado algo (crash del port), rescate por HID."""
        with self.lock:
            dead = self.applied and time.perf_counter() - self.last_ack > RESCUE_AFTER_S
        ok = self._send(F_RELEASE, None, None, None) if self.target else False
        if dead or (self.applied and not self.target):
            self.rescue()
        with self.lock:
            self.applied = False
        return ok

    def rescue(self) -> None:
        if not self.rescue_enabled:
            return
        if dsx_running():
            self.say("PORT  el port se fue sin soltar los gatillos; DSX está abierto, él los gestionará")
            return
        try:
            from unds_hid import HidClient
            client = HidClient(self.say)
        except (ImportError, RuntimeError) as error:
            self.say(f"PORT  el port se cerró sin 'bye' y los gatillos pueden quedar duros; no hay HID para "
                     f"soltarlos ({error}). Desconecta y reconecta el mando, o abre el juego otra vez.")
            return
        try:
            if client.send([(RESET, [0])]):
                self.rescues += 1
                self.say("PORT  el port se cerró sin 'bye': gatillos soltados por HID directo (rescate)")
        finally:
            client.close()

    # -- acks (hilo del listener) --
    def on_ack(self, ev: dict) -> None:
        now = time.perf_counter()
        ok = int(ev.get("ok", 1))
        why = str(ev.get("why", "cmd"))
        with self.lock:
            self.acks += 1
            self.last_ack = now
            self.ack_results[ok] += 1
            sent_at = self.pending.pop(int(ev.get("c", 0)), None)
            if sent_at is not None:
                self.rtt_ms.append((now - sent_at) * 1000.0)
                self.port_us.append(int(ev.get("q", 0)))
            first = ok != 1 and ok not in self.ack_said
            if first:
                self.ack_said.add(ok)
        if first:
            self.say(f"PORT  {ACK_TEXT.get(ok, f'resultado {ok}')}")
        if why == "reconnect":
            self.say("PORT  el port (re)abrió el mando: reaplicó gatillos y lightbar")
        elif why == "timeout":
            self.say("PORT  el port soltó los gatillos por silencio (> 5 s sin datagramas del companion)")

    def note_alive(self) -> None:
        """Cualquier evento del port activo cuenta como vida (el rescate solo salta con el port muerto)."""
        with self.lock:
            self.last_ack = max(self.last_ack, time.perf_counter() - 1.0)

    # -- interfaz del motor --
    def status(self, timeout: float = 1.0) -> Response | None:
        with self.lock:
            if self.target is None:
                return None
            device = {"Index": 0, "DeviceType": 0, "ConnectionType": 0, "BatteryLevel": "-", "IsSupportAT": True,
                      "IsSupportLightBar": True, "MacAddress": f"port pid {self.target_pid}"}
        return Response(rx=time.perf_counter(), data={"Devices": [device]}, raw="", after="port", rtt_ms=None)

    def response_count(self) -> int:
        return self.acks

    @staticmethod
    def reset(index: int) -> tuple[int, list[int]]:
        return RESET, [index]

    def latency_text(self) -> str:
        with self.lock:
            rtt = sorted(self.rtt_ms)
            inside = sorted(self.port_us)
            results = dict(self.ack_results)
        if not rtt:
            return "sin acks del port"
        p = lambda xs, q: xs[min(len(xs) - 1, int(q * len(xs)))]  # noqa: E731
        bad = ", ".join(f"ok={k}: {v}" for k, v in sorted(results.items()) if k != 1)
        return (f"ida y vuelta companion->port->SDL->ack p50 {p(rtt, .5):.2f} ms p90 {p(rtt, .9):.2f} ms "
                f"máx {rtt[-1]:.2f} ms; dentro del port p50 {p(inside, .5)} us"
                + (f"; {bad}" if bad else ""))

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ---- prueba guiada --------------------------------------------------------------------------------
def cmd_test(port: int) -> int:
    """Escucha el bus como el companion (el companion debe estar CERRADO: mismo puerto), espera un hello y
    prueba R2, L2 y la lightbar por el port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError as error:
        print(f"No puedo escuchar en 127.0.0.1:{port} ({error}). ¿unds.py abierto? Ciérralo para esta prueba.")
        return 2
    sock.settimeout(0.2)
    client = PortClient()
    acks: list[dict] = []
    stop = threading.Event()
    hello: dict = {}

    def reader() -> None:
        import json
        while not stop.is_set():
            try:
                data, source = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                ev = json.loads(data.decode("utf-8"))
            except ValueError:
                continue
            if ev.get("e") == "hello" and not hello:
                hello.update(ev)
                client.retarget(source, ev.get("pid", 0), ev)
            elif ev.get("e") == "fx":
                acks.append(ev)
                client.on_ack(ev)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    print("Esperando el hello del port (abre el juego con UNDS_EVENTS=127.0.0.1:%d y entra en partida)..." % port)
    while not hello:
        time.sleep(0.2)
    if not client.target:
        stop.set()
        return 1
    steps = [
        ("R2 = WEAPON 3 4 6: aprieta R2, debe haber un tope que se rompe", "R", "WEAPON 3 4 6", None),
        ("L2 = FEEDBACK 2 1: resistencia suave y constante", "L", "FEEDBACK 2 1", None),
        ("R2 = VIBRATION 1 8 40: R2 vibra al apretarlo", "R", "VIBRATION 1 8 40", None),
        ("lightbar ROJA", None, None, (255, 0, 0)),
        ("lightbar VERDE", None, None, (0, 255, 0)),
    ]
    try:
        for text, side, effect, rgb in steps:
            input(f"\n>> {text}  [Enter]")
            before = len(acks)
            if side:
                e = parse_effect(effect)
                client.send([(TRIGGER_UPDATE, [0, RIGHT if side == "R" else LEFT, e.mode, *e.params])])
            else:
                client.send([(RGB_UPDATE, [0, *rgb, 255])])
            time.sleep(0.3)
            got = acks[before:]
            print("   ack: " + (", ".join(f"ok={a.get('ok')} q={a.get('q')} us" for a in got) if got else "NINGUNO"))
        input("\n>> soltar (gatillos libres, lightbar del juego)  [Enter]")
        client.send([(RESET, [0])])
        time.sleep(0.3)
        print(client.latency_text())
        print("Listo si: cada paso se sintió/vio como se anunció, todos los ack con ok=1 y los hápticos siguen "
              "funcionando con el juego.")
        return 0
    except KeyboardInterrupt:
        client.send([(RESET, [0])])
        return 130
    finally:
        stop.set()
        thread.join(timeout=1.0)
        sock.close()
        client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gatillos y lightbar del DualSense a través del port (canal fx).")
    parser.add_argument("--test", action="store_true", help="prueba guiada (juego abierto, unds.py cerrado)")
    parser.add_argument("--port", type=int, default=47111, help="puerto UDP del bus UNDS (47111)")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    if args.test:
        return cmd_test(args.port)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
