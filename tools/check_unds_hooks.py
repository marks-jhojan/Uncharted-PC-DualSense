"""Comprueba los hooks de estado UNDS contra un ejecutable, como lo hará el loader del port (v0.3.7+).

Lee los patrones del propio src/runtime_unds.c (tabla u1_state_callbacks: nombre, patrón, skip, moved)
y los busca en los segmentos PT_LOAD ejecutables del ELF, igual que probe.c find_pattern(): bytes en
hexadecimal, "??" = cualquier byte, y cada patrón debe aparecer UNA sola vez.

Solo lee. Uso (CMD, Python 3.12 de python.org):
  python check_unds_hooks.py <checkout del port>\\src\\runtime_unds.c <ejecutable> [<ejecutable> ...]
Ejecutables válidos: el eboot.bin del dump (ELF descifrado de Itemzflow) o el que el launcher deja en
<build>\\out\\eboot\\eboot.elf. Un SELF cifrado no sirve.

Resultado por hook: "OK 0x... (1 coincidencia)" / "NO ENCONTRADO" / "AMBIGUO (n)". Con la 1.02 lo
esperado es OK en los cuatro; si no, pásame la salida y el ejecutable se analiza en local.
"""
import hashlib
import re
import struct
import sys
from pathlib import Path

ENTRY = re.compile(r'\{\s*"(UNDS [^"]+)",\s*((?:"[^"]*"\s*)+),\s*(\d+)\s*,\s*(\d+)\s*,', re.S)


def load_callbacks(source):
    text = Path(source).read_text(encoding='utf-8', errors='replace')
    start = text.find('u1_state_callbacks[]')
    if start < 0:
        sys.exit(f'{source}: no encuentro la tabla u1_state_callbacks (¿parches UNDS para v0.3.8 aplicados?)')
    table = text[start:text.find('};', start)]
    out = []
    for name, pieces, skip, moved in ENTRY.findall(table):
        pattern = ''.join(re.findall(r'"([^"]*)"', pieces)).split()
        out.append((name, pattern, int(skip), int(moved)))
    if not out:
        sys.exit(f'{source}: tabla vacía o con un formato inesperado')
    return out


def exec_segments(data, path):
    if data[:4] != b'\x7fELF':
        sys.exit(f'{path}: no es un ELF (¿SELF cifrado?)')
    phoff, = struct.unpack_from('<Q', data, 0x20)
    size, count = struct.unpack_from('<HH', data, 0x36)
    segments = []
    for i in range(count):
        kind, flags, offset, vaddr, _paddr, filesz, _memsz, _align = struct.unpack_from('<IIQQQQQQ', data, phoff + i * size)
        if kind == 1 and flags & 1:
            segments.append((vaddr, data[offset:offset + filesz]))
    return segments


def find(segments, pattern):
    regex = re.compile(b''.join(b'.' if t == '??' else re.escape(bytes([int(t, 16)])) for t in pattern), re.S)
    hits = []
    for vaddr, code in segments:
        pos = 0
        while (m := regex.search(code, pos)) is not None:   # solapadas también, como probe.c
            hits.append(vaddr + m.start())
            pos = m.start() + 1
    return hits


def main(argv):
    if len(argv) < 3:
        sys.exit(__doc__)
    callbacks = load_callbacks(argv[1])
    failures = 0
    for path in argv[2:]:
        data = Path(path).read_bytes()
        print(f'== {path}\n   sha256 {hashlib.sha256(data).hexdigest()}  {len(data) / 2**20:.1f} MB')
        segments = exec_segments(data, path)
        for name, pattern, skip, moved in callbacks:
            hits = find(segments, pattern)
            if len(hits) == 1:
                print(f'   OK          {name:36s} hook {hits[0] + skip:#x} (1 coincidencia, {len(pattern)} bytes)')
            else:
                failures += 1
                state = 'NO ENCONTRADO' if not hits else f'AMBIGUO ({len(hits)})'
                print(f'   {state:11s} {name:36s} ' + ' '.join(f'{h + skip:#x}' for h in hits[:4]))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
