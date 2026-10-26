#!/usr/bin/env python3
"""UNDS — reenvía una grabación JSONL al bus (UDP) con su temporización original.

Sirve para ajustar gatillos y hápticos sin jugar: unds.py recibe los eventos como si vinieran del
port. Reescribe seq (consecutivo) y t (reloj actual) para que las estadísticas de pérdidas y
latencia del companion sigan teniendo sentido, y marca cada evento con "replay": 1. Antes de la
ventana pedida envía el último hello y el último estado de pad/led/rumble, para que el companion
arranque sincronizado; usa un pid propio por reproducción. Al terminar (o con Ctrl+C) envía un bye.

    python unds_replay.py                                   # última grabación de .\\recordings\\
    python unds_replay.py recordings\\unds_20261022_010344.jsonl --from 2 --to 7   # 5 disparos de pistola
    python unds_replay.py --list-fires                      # momentos con firma de disparo (para --from)

Solo biblioteca estándar.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path


def load(path: Path) -> list[dict]:
    events = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            ev = item.get("ev")
            if isinstance(ev, dict) and isinstance(ev.get("t"), int) and item.get("src") not in ("dsx", "hap"):
                events.append(ev)
    events.sort(key=lambda e: e["t"])
    return events


def latest_recording(folder: Path) -> Path | None:
    files = sorted(folder.glob("unds_*.jsonl"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Reenvía una grabación UNDS al bus UDP.")
    parser.add_argument("file", nargs="?", help="grabación JSONL (por defecto la última de recordings/)")
    parser.add_argument("--from", dest="t_from", type=float, default=0.0, help="segundo inicial (desde el primer evento)")
    parser.add_argument("--to", dest="t_to", type=float, default=None, help="segundo final")
    parser.add_argument("--speed", type=float, default=1.0, help="velocidad (1.0)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=47111)
    parser.add_argument("--types", default="all", help="tipos a enviar: all o lista (hello,bye,rumble,led,pad,aopen,audio,gs,shot,dmg)")
    parser.add_argument("--list-fires", action="store_true", help="lista los rumbles con firma de disparo y sale")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    path = Path(args.file) if args.file else latest_recording(here / "recordings")
    if path is None or not path.exists():
        print(f"no encuentro la grabación ({path or here / 'recordings'})", file=sys.stderr)
        return 2
    events = load(path)
    if not events:
        print(f"{path}: sin eventos", file=sys.stderr)
        return 2
    t0 = events[0]["t"]
    span = (events[-1]["t"] - t0) / 1e6
    if args.list_fires:
        prev = (0, 0)
        for ev in events:
            if ev.get("e") == "rumble" and not ev.get("re"):
                cur = (ev.get("l", 0), ev.get("s", 0))
                if cur[1] >= 200 and 76 <= cur[0] <= 120 and not (prev[1] >= 200 and 76 <= prev[0] <= 120):
                    print(f"  {(ev['t'] - t0) / 1e6:9.3f} s  rumble {cur}")
                prev = cur
        return 0
    types = None if args.types == "all" else {t.strip() for t in args.types.split(",")}
    start_us = t0 + int(args.t_from * 1e6)
    end_us = None if args.t_to is None else t0 + int(args.t_to * 1e6)
    # estado previo a la ventana: último hello y último pad / led / rumble / gs
    prior: dict[str, dict] = {}
    for ev in events:
        if ev["t"] >= start_us:
            break
        if ev.get("e") in ("hello", "pad", "led", "rumble", "aopen", "gs"):
            prior[ev["e"] if ev.get("e") != "aopen" else f"aopen{ev.get('p')}"] = ev
    window = [ev for ev in events if ev["t"] >= start_us and (end_us is None or ev["t"] <= end_us)
              and ev.get("e") != "bye" and (types is None or ev.get("e") in types)]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    target = (args.host, args.port)
    seq = 0
    # pid propio por reproducción: el companion ve un emisor nuevo cada vez (un pid que ya hizo bye
    # se ignora, y repetir la misma grabación no volvería a enganchar).
    replay_pid = 900000 + os.getpid() % 100000

    def send(ev: dict) -> None:
        nonlocal seq
        out = dict(ev)
        out["seq"] = seq
        out["t"] = time.perf_counter_ns() // 1000
        out["replay"] = 1
        if out.get("e") == "hello":
            out["pid"] = replay_pid
            for key in ("smax", "err", "drop"):     # contadores del emisor original: no son de esta reproducción
                out.pop(key, None)
        seq += 1
        try:
            sock.sendto(json.dumps(out, separators=(",", ":")).encode("utf-8"), target)
        except OSError as error:
            print(f"sendto: {error}", file=sys.stderr)

    print(f"{path.name}: {len(events)} eventos, {span:.1f} s. Envío {len(window)} eventos de "
          f"{args.t_from:.2f} s a {args.t_to if args.t_to is not None else span:.2f} s a {args.host}:{args.port} "
          f"(x{args.speed}).")
    hello = prior.pop("hello", None) or next((e for e in events if e.get("e") == "hello"), None)
    if hello is None:
        print("la grabación no tiene hello: el companion no sabrá a qué juego atribuir los eventos", file=sys.stderr)
    else:
        send(hello)
    for ev in prior.values():
        send(ev)
    time.sleep(0.2)
    origin = time.perf_counter()
    last_hello = origin
    sent = 0
    try:
        for ev in window:
            due = origin + (ev["t"] - start_us) / 1e6 / args.speed
            while True:
                now = time.perf_counter()
                if now >= due:
                    break
                if hello is not None and now - last_hello >= 1.0:   # latido para que no lo dé por muerto
                    send(hello)
                    last_hello = now
                time.sleep(min(0.002, due - now))
            if ev.get("e") == "hello":
                last_hello = time.perf_counter()
            send(ev)
            sent += 1
    except KeyboardInterrupt:
        print("interrumpido")
    finally:
        send({"v": 1, "e": "bye", "reason": "exit", "up": int((time.perf_counter() - origin) * 1e6), "sent": seq + 1})
        sock.close()
    print(f"enviados {sent} eventos de la ventana (+ estado inicial y bye)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
