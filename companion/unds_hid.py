#!/usr/bin/env python3
"""UNDS — gatillos adaptativos y lightbar del DualSense por HID directo (USB), sin DSX.

Por qué: DSX, abierto, activa el rumble de compatibilidad del mando y eso desactiva los hápticos
por audio (verificado en hardware el 2026-10-22). Este módulo escribe él mismo el reporte de
salida y SOLO marca los bits de gatillos y lightbar; nunca los de rumble, así que los hápticos
siguen funcionando (y un reporte sin esos bits los reactiva si alguien los había apagado).

Reporte de salida USB (id 0x02, 48 bytes). Fuente: SDL 3.4.10, SDL_hidapi_ps5.c, DS5EffectsState_t
(:163-185, desplazado +1 por el id) y test/testcontroller.c:526; coincide con dualsense-controller
0.3.1 (Usb01OutReport.py):
    [0] 0x02   [1] EnableBits1: 0x04 gatillo derecho, 0x08 gatillo izquierdo
               (0x01/0x02 = rumble emulado / "Disable audio haptics": NUNCA se ponen aquí)
    [2] EnableBits2: 0x04 color de la lightbar
    [11..21] efecto de R2 (11 bytes)   [22..32] efecto de L2   [45..47] R G B
Efectos "oficiales" (factorías de Nielk1, MIT; portadas en dualsense-controller), zonas 0-9:
    OFF 0x05 | FEEDBACK 0x21 | WEAPON 0x25 | VIBRATION 0x26
    byte 0 modo, 1-2 máscara de zonas activas, 3-6 fuerza de 3 bits por zona, 9 frecuencia.
Efecto "no oficial" (misma factoría, Machine(); puede desaparecer con un firmware futuro):
    MACHINE 0x27: 1-2 máscara (1<<inicio)|(1<<fin), 3 fuerza A (bits 0-2) | fuerza B (bits 3-5),
    4 frecuencia en Hz, 5 periodo en décimas de segundo, 6-10 a cero. Oscila entre A y B.
Los efectos se escriben con la misma sintaxis que en DSX (unds_triggers.ini no cambia).
[HIPÓTESIS] DSX numera las zonas igual (sus rangos WEAPON 2-7/3-8 son idénticos a la factoría).
Comprobación: `python unds.py --calibrate` debe dar la misma pared que con DSX (r2a ~24-67).

    python unds_hid.py --list                    mandos DualSense visibles por HID
    python unds_hid.py --test                    prueba guiada: R2, L2, lightbar
    python unds_hid.py --set R "WEAPON 3 4 6"    un efecto (L o R)
    python unds_hid.py --rgb 255 0 0             lightbar
    python unds_hid.py --off                     gatillos libres y lightbar azul

Requiere: python -m pip install hidapi   (DSX cerrado)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from unds_dsx import (LEFT, RGB_UPDATE, RESET, RIGHT, TRIGGER_UPDATE, V3_MODES, Response,  # noqa: E402
                      parse_effect)

try:
    import hid  # paquete "hidapi" (cython-hidapi)
except ImportError as _error:
    hid = None
    HID_ERROR: Exception | None = _error
else:
    HID_ERROR = None

VENDOR_SONY = 0x054C                                    # SDL usb_ids.h
PRODUCTS = {0x0CE6: "DualSense", 0x0DF2: "DualSense Edge"}
BUS_USB, BUS_BLUETOOTH = 0x01, 0x02                     # hidapi.h hid_bus_type
REPORT_ID, REPORT_LEN = 0x02, 48
OFF_BITS1, OFF_BITS2, OFF_RIGHT, OFF_LEFT, OFF_RGB = 1, 2, 11, 22, 45
BIT_RIGHT, BIT_LEFT, BIT_LED = 0x04, 0x08, 0x04
RELEASE_RGB = (0, 0, 64)                                # al soltar el mando: azul tenue
MODE_BY_DSX = {mode: name for name, (mode, _) in V3_MODES.items()}


# ---- codificación de efectos ----------------------------------------------------------------------
def _zones(strengths: list[int]) -> tuple[int, int]:
    """10 fuerzas 0-8 -> (máscara de zonas activas, fuerzas de 3 bits empaquetadas)."""
    active = packed = 0
    for i, s in enumerate(strengths):
        if s > 0:
            active |= 1 << i
            packed |= ((s - 1) & 0x07) << (3 * i)
    return active, packed


def _official(mode: int, active: int, packed: int, frequency: int = 0) -> bytes:
    out = bytearray(11)
    out[0] = mode
    out[1], out[2] = active & 0xFF, (active >> 8) & 0xFF
    out[3:7] = packed.to_bytes(4, "little")
    out[9] = frequency & 0xFF
    return bytes(out)


def encode_effect(dsx_mode: int, params: tuple[int, ...]) -> bytes:
    """Modo v3 de DSX (20-26, ya validado por parse_effect) -> 11 bytes del efecto de gatillo."""
    name = MODE_BY_DSX.get(dsx_mode)
    if name is None:
        raise ValueError(f"el modo {dsx_mode} es un modo heredado de DSX v2: no existe en HID directo")
    if name == "OFF":
        return bytes([0x05]) + bytes(10)
    if name == "FEEDBACK":
        start, strength = params
        return _official(0x21, *_zones([strength if i >= start else 0 for i in range(10)]))
    if name == "WEAPON":
        start, end, strength = params
        return _official(0x25, (1 << start) | (1 << end), (strength - 1) & 0x07)
    if name == "VIBRATION":
        start, amplitude, frequency = params
        return _official(0x26, *_zones([amplitude if i >= start else 0 for i in range(10)]), frequency)
    if name == "SLOPE_FEEDBACK":
        start, end, s0, s1 = params
        slope = (s1 - s0) / (end - start)
        strengths = [0] * 10
        for i in range(start, 10):
            strengths[i] = round(s0 + slope * (i - start)) if i <= end else s1
        return _official(0x21, *_zones(strengths))
    if name == "MULTIPLE_POSITION_FEEDBACK":
        active, packed = _zones(list(params))
        return _official(0x21, active, packed) if active else bytes([0x05]) + bytes(10)
    if name == "MULTIPLE_POSITION_VIBRATION":
        frequency, *amplitudes = params
        active, packed = _zones(amplitudes)
        return _official(0x26, active, packed, frequency) if active else bytes([0x05]) + bytes(10)
    if name == "MACHINE":
        start, end, amp_a, amp_b, frequency, period = params
        out = bytearray(11)
        mask = (1 << start) | (1 << end)
        out[0], out[1], out[2] = 0x27, mask & 0xFF, (mask >> 8) & 0xFF
        out[3] = (amp_a & 0x07) | ((amp_b & 0x07) << 3)
        out[4], out[5] = frequency & 0xFF, period & 0xFF
        return bytes(out)
    raise ValueError(f"modo {name} sin codificar")


def build_report(right: bytes | None = None, left: bytes | None = None,
                 rgb: tuple[int, int, int] | None = None) -> bytes:
    """Reporte con solo los campos pedidos; los demás quedan sin bit de validez (el mando los ignora)."""
    report = bytearray(REPORT_LEN)
    report[0] = REPORT_ID
    if right is not None:
        report[OFF_BITS1] |= BIT_RIGHT
        report[OFF_RIGHT:OFF_RIGHT + 11] = right
    if left is not None:
        report[OFF_BITS1] |= BIT_LEFT
        report[OFF_LEFT:OFF_LEFT + 11] = left
    if rgb is not None:
        report[OFF_BITS2] |= BIT_LED
        report[OFF_RGB:OFF_RGB + 3] = bytes(max(0, min(255, int(c))) for c in rgb)
    return bytes(report)


# ---- DSX abierto ----------------------------------------------------------------------------------
def dsx_running() -> bool:
    """True si DSX.exe está en ejecución (solo Windows; tasklist)."""
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq DSX.exe", "/NH", "/FO", "CSV"],
                             capture_output=True, text=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return '"dsx.exe"' in out.lower()


# ---- dispositivo ----------------------------------------------------------------------------------
def find_controllers() -> list[dict]:
    if hid is None:
        return []
    found = []
    for pid in PRODUCTS:
        for info in hid.enumerate(VENDOR_SONY, pid):
            info = dict(info)
            info["model"] = PRODUCTS[pid]
            found.append(info)
    # primero USB y la colección de gamepad (usage page 1, usage 5)
    found.sort(key=lambda d: (d.get("bus_type") != BUS_USB, (d.get("usage_page"), d.get("usage")) != (1, 5)))
    return found


class HidClient:
    """Interfaz compatible con DSXClient para el motor de gatillos (send/status/close y contadores),
    pero escribiendo el reporte HID al mando. Reabre solo tras desconexiones."""

    label = "HID"

    def __init__(self, say=print):
        if HID_ERROR is not None:
            raise RuntimeError(f"falta el paquete hidapi ({HID_ERROR}): python -m pip install hidapi")
        self.say = say
        self.port = "USB"
        self.port_source = "HID directo"
        self.sent = 0
        self.send_errors = 0
        self.reset_errors = 0
        self.device = None
        self.info: dict | None = None
        self.lock = threading.Lock()
        self.next_try = 0.0
        self.last_problem: str | None = None

    # -- conexión --
    def _open(self) -> bool:
        now = time.perf_counter()
        if self.device is not None:
            return True
        if now < self.next_try:
            return False
        self.next_try = now + 1.0
        controllers = find_controllers()
        usb = [c for c in controllers if c.get("bus_type") == BUS_USB]
        if not usb:
            problem = ("el DualSense está por Bluetooth: conéctalo por USB (los hápticos y este reporte son USB)"
                       if controllers else
                       "no veo ningún DualSense por HID (¿USB desconectado? ¿HidHide lo oculta a Python?)")
            self._problem(problem)
            return False
        info = usb[0]
        device = hid.device()
        try:
            device.open_path(info["path"])
        except (OSError, IOError) as error:
            self._problem(f"no puedo abrir el {info['model']} ({error})")
            return False
        self.device, self.info, self.last_problem = device, info, None
        self.say(f"HID   {info['model']} por USB abierto (serie {info.get('serial_number') or '-'})")
        return True

    def _problem(self, text: str) -> None:
        if text != self.last_problem:
            self.say(f"HID   {text}")
        self.last_problem = text

    def _drop(self) -> None:
        if self.device is not None:
            try:
                self.device.close()
            except Exception:  # noqa: BLE001
                pass
        self.device = None

    def write(self, report: bytes) -> bool:
        with self.lock:
            if not self._open():
                self.send_errors += 1
                return False
            try:
                written = self.device.write(report)
            except (OSError, ValueError) as error:
                written = -1
                self._problem(f"error al escribir ({error})")
            if written < 0:
                self.send_errors += 1
                self._drop()
                self._problem("escritura rechazada: el mando se desconectó; reintento cada 1 s")
                return False
            self.sent += 1
            return True

    # -- interfaz DSXClient --
    def send(self, instructions: list[tuple[int, list[int]]]) -> bool:
        right = left = rgb = None
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
                right = left = encode_effect(20, ())
                rgb = RELEASE_RGB
        if right is None and left is None and rgb is None:
            return True
        return self.write(build_report(right, left, rgb))

    def status(self, timeout: float = 1.0) -> Response | None:
        with self.lock:
            ok = self._open()
            info = self.info
        if not ok or info is None:
            return None
        device = {"Index": 0, "DeviceType": 1 if info["product_id"] == 0x0DF2 else 0, "ConnectionType": 0,
                  "BatteryLevel": "-", "IsSupportAT": True, "IsSupportLightBar": True,
                  "MacAddress": info.get("serial_number") or "-"}
        return Response(rx=time.perf_counter(), data={"Devices": [device]}, raw="", after="hid", rtt_ms=None)

    def response_count(self) -> int:
        return self.sent

    @staticmethod
    def reset(index: int) -> tuple[int, list[int]]:
        return RESET, [index]

    def close(self) -> None:
        with self.lock:
            self._drop()


# ---- CLI ------------------------------------------------------------------------------------------
def cmd_list() -> int:
    controllers = find_controllers()
    if not controllers:
        print("No veo ningún DualSense por HID.")
        return 1
    for c in controllers:
        bus = {BUS_USB: "USB", BUS_BLUETOOTH: "Bluetooth"}.get(c.get("bus_type"), c.get("bus_type"))
        print(f"  {c['model']} por {bus}  usage {c.get('usage_page')}/{c.get('usage')}  serie {c.get('serial_number')}  "
              f"{c['path'].decode(errors='replace')}")
    return 0


def cmd_test(client: HidClient) -> int:
    steps = [
        ("R2 = WEAPON 3 4 6: aprieta R2, debe haber un tope que se rompe", "R", "WEAPON 3 4 6", None),
        ("L2 = FEEDBACK 2 1: resistencia suave y constante", "L", "FEEDBACK 2 1", None),
        ("R2 = VIBRATION 1 8 40: R2 vibra al apretarlo", "R", "VIBRATION 1 8 40", None),
        ("lightbar ROJA", None, None, (255, 0, 0)),
        ("lightbar VERDE", None, None, (0, 255, 0)),
    ]
    for text, side, effect, rgb in steps:
        input(f"\n>> {text}  [Enter para seguir]")
        if side:
            e = parse_effect(effect)
            ok = client.send([(TRIGGER_UPDATE, [0, RIGHT if side == "R" else LEFT, e.mode, *e.params])])
        else:
            ok = client.send([(RGB_UPDATE, [0, *rgb, 255])])
        print("   enviado" if ok else "   NO enviado (mira el mensaje HID de arriba)")
    input("\n>> soltar todo (gatillos libres, lightbar azul)  [Enter]")
    client.send([(RESET, [0])])
    print("Listo si: cada paso se sintió/vio como se anunció y, con unds_haptics.py --test a la vez, "
          "los hápticos siguen funcionando.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gatillos y lightbar del DualSense por HID directo (USB).")
    parser.add_argument("--list", action="store_true", help="lista los DualSense visibles")
    parser.add_argument("--test", action="store_true", help="prueba guiada")
    parser.add_argument("--set", nargs=2, metavar=("L|R", "EFECTO"), help='p. ej. --set R "WEAPON 3 4 6"')
    parser.add_argument("--rgb", nargs=3, type=int, metavar=("R", "G", "B"), help="color de la lightbar")
    parser.add_argument("--off", action="store_true", help="gatillos libres y lightbar azul")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    if HID_ERROR is not None:
        print(f"falta hidapi ({HID_ERROR}):  python -m pip install hidapi", file=sys.stderr)
        return 2
    if dsx_running():
        print("AVISO: DSX está abierto. Ciérralo: pelea por los gatillos y apaga los hápticos.")
    if args.list:
        return cmd_list()
    client = HidClient()
    try:
        if args.test:
            return cmd_test(client)
        if args.set:
            side, text = args.set
            e = parse_effect(text)
            return 0 if client.send([(TRIGGER_UPDATE, [0, LEFT if side.upper().startswith("L") else RIGHT,
                                                       e.mode, *e.params])]) else 1
        if args.rgb:
            return 0 if client.send([(RGB_UPDATE, [0, *args.rgb, 255])]) else 1
        if args.off:
            return 0 if client.send([(RESET, [0])]) else 1
        parser.print_help()
        return 0
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
