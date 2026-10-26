#!/usr/bin/env python3
"""UNDS — revisa un dump de Uncharted: The Nathan Drake Collection (CUSA02320 y similares).

Dice, para cada carpeta (base, parche o combinada):
  - la versión que declara sce_sys/param.sfo (APP_VER);
  - si están los tres ejecutables (eboot.bin = U1, big2-ps4_Shipping.elf = U2, big3-ps4_Shipping.elf = U3),
    su formato (ELF descifrado de Itemzflow / SELF / vacío) y tamaño;
  - la versión REAL del código de cada ejecutable ELF (1.00 o 1.02), comprobando las instrucciones que
    parchean los cheats públicos de GoldHEN para cada versión (CUSA02320_01.00 / _01.02);
  - si los archivos de datos del parche (u1data, u2data, u3data) existen.

    python check_dump.py D:\\Juegos\\CUSA02320 D:\\Juegos\\CUSA02320-patch
    python check_dump.py D:\\Juegos\\CUSA02320-1.02          # un dump combinado

Solo lee. Solo biblioteca estándar (Python 3.8+).
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

EXES = ("eboot.bin", "big2-ps4_Shipping.elf", "big3-ps4_Shipping.elf")
GAME = {"eboot.bin": "U1", "big2-ps4_Shipping.elf": "U2", "big3-ps4_Shipping.elf": "U3"}
# (dirección de imagen, bytes originales) de los cheats GoldHEN "Task View" / "Dev Menu" de cada versión
# (GoldHEN_Cheat_Repository: CUSA02320_01.00*.json/.shn y CUSA02320_01.02*.json/.shn).
SIGNATURES = {
    "eboot.bin": {"1.00": [(0x2656C5, "80BB4448000000"), (0x2CF8B8, "0F85AC0E0000")],
                  "1.02": [(0x265C35, "80BB4448000000"), (0x2CFEA8, "0F85AC0E0000")]},
    "big2-ps4_Shipping.elf": {"1.00": [(0x332721, "80B88C48000000"), (0x14C504, "0F85C10B0000")],
                              "1.02": [(0x4B443F, "418A868C480000"), (0x3F8F54, "0F85741A0000")]},
    "big3-ps4_Shipping.elf": {"1.00": [(0xDAFAE0, "80B8FC4B000000"), (0x162194, "0F85C10B0000")],
                              "1.02": [(0xAB5243, "80B8FC4B000000"), (0x56CB94, "0F85551A0000")]},
}


def sfo_values(path: Path) -> dict:
    try:
        data = path.read_bytes()
        magic, _version, keys, values, count = struct.unpack_from("<IIIII", data, 0)
        if magic != 0x46535000:
            return {}
        out = {}
        for i in range(count):
            key_off, fmt, length, _max, data_off = struct.unpack_from("<HHIII", data, 20 + 16 * i)
            key = data[keys + key_off:data.index(b"\0", keys + key_off)].decode("ascii", "replace")
            raw = data[values + data_off:values + data_off + length]
            out[key] = struct.unpack("<I", raw)[0] if fmt == 0x0404 else raw.rstrip(b"\0").decode("utf-8", "replace")
        return out
    except (OSError, struct.error, ValueError):
        return {}


def image_reader(data: bytes):
    """Lector de direcciones de imagen de un ELF64 (segmentos LOAD y el de datos relro de Sony)."""
    phoff = struct.unpack_from("<Q", data, 0x20)[0]
    count = struct.unpack_from("<H", data, 0x38)[0]
    segments = []
    for i in range(count):
        kind, _flags, offset, vaddr, _paddr, filesz, _memsz, _align = struct.unpack_from("<IIQQQQQQ", data, phoff + 56 * i)
        if kind in (1, 0x61000010) and filesz:
            segments.append((vaddr, offset, filesz))

    def read(address: int, size: int) -> bytes:
        for vaddr, offset, filesz in segments:
            if vaddr <= address and address + size <= vaddr + filesz:
                return data[offset + address - vaddr:offset + address - vaddr + size]
        return b""
    return read


def exe_report(path: Path, name: str) -> str:
    if not path.exists():
        return "FALTA"
    size = path.stat().st_size
    if size == 0:
        return "vacío (0 bytes)"
    head = path.open("rb").read(4)
    if head == b"\x4f\x15\x3d\x1d":
        return f"SELF cifrado/firmado, {size / 1e6:.1f} MB (versión no comprobable aquí)"
    if head != b"\x7fELF":
        return f"formato desconocido ({head.hex()}), {size / 1e6:.1f} MB"
    data = path.read_bytes()
    try:
        read = image_reader(data)
    except struct.error:
        return f"ELF dañado, {size / 1e6:.1f} MB"
    found = [ver for ver, sigs in SIGNATURES[name].items()
             if all(read(addr, len(bytes.fromhex(b))) == bytes.fromhex(b) for addr, b in sigs)]
    version = " y ".join(found) if found else "ni 1.00 ni 1.02 (otra versión o región)"
    return f"ELF descifrado, {size / 1e6:.1f} MB, código {version}"


def check(folder: Path) -> None:
    print(f"\n== {folder}")
    if not folder.is_dir():
        print("   no existe")
        return
    sfo = sfo_values(folder / "sce_sys" / "param.sfo")
    if sfo:
        print(f"   param.sfo: {sfo.get('TITLE_ID', '?')} APP_VER {sfo.get('APP_VER', '?')} "
              f"(CATEGORY {sfo.get('CATEGORY', '?')}: gd = juego base, gp = parche)")
    else:
        print("   sin sce_sys/param.sfo legible")
    for name in EXES:
        print(f"   {GAME[name]} {name:<24} {exe_report(folder / name, name)}")
        fself = folder / (name + ".fself")
        if fself.exists():
            print(f"      {fself.name}: {fself.stat().st_size} bytes")
    for data in ("u1data", "u2data", "u3data"):
        path = folder / data
        if path.is_dir():
            files = [p for p in path.rglob("*") if p.is_file()]
            total = sum(p.stat().st_size for p in files)
            print(f"   {data}: {len(files)} archivos, {total / 1e6:.0f} MB")


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    if not argv:
        print(__doc__)
        return 2
    for arg in argv:
        p = Path(arg)
        if p.is_file():                       # un ejecutable suelto
            name = next((n for n in EXES if p.name == n), None)
            if name is None:
                for n in EXES:                # eboot.elf del port, etc.: se prueban las tres firmas
                    r = exe_report(p, n)
                    if "código 1." in r:
                        print(f"{p}: como {GAME[n]} -> {r}")
                        break
                else:
                    print(f"{p}: {exe_report(p, EXES[0])}")
            else:
                print(f"{p}: {exe_report(p, name)}")
        else:
            check(p)
    print("\nUn dump 1.02 completo debe tener los TRES ejecutables con 'código 1.02'.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
