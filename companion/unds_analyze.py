#!/usr/bin/env python3
"""UNDS — análisis de una grabación de unds_listen.py (criterio de "listo" del Día 2).

Busca, para un emisor (por defecto el de más eventos, normalmente U2):
  * disparos de R2: flanco de subida del bit R2 o de r2 >= --r2-threshold;
  * pulsos de rumble: paso de (0,0) a algo distinto de cero, con su duración y su pico;
  * picos de audio en el puerto main (0): subida de RMS >= --audio-rise dB sobre la mediana de los
    300 ms previos, por encima de --audio-floor dBFS, con 60 ms de periodo refractario;
y los cruza:
  * qué pulsos de rumble caen con R2 mantenido (o hasta --after-release ms después de soltarlo);
  * qué pulsos tienen un pico de audio a menos de --audio-window ms.

    python unds_analyze.py                      (la grabación más reciente de .\\recordings)
    python unds_analyze.py recordings\\unds_20261021_213000.jsonl
    python unds_analyze.py rec.jsonl --from 30 --to 75 --plot tiroteo.png   (matplotlib opcional)

Los umbrales son heurísticos (no hay verdad de referencia): ajústalos si el tiroteo lo pide y
mira la gráfica. Los tiempos son los "t" del emisor (CLOCK_MONOTONIC del port, us). El audio se
oye ~2 buffers después de su "t" (cola de SDL) y el rumble de SDL sale al instante.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

BTN_R2 = 0x200
BTN_L2 = 0x100


def db(level: int) -> float:
    return 20.0 * math.log10(level / 10000.0) if level > 0 else -120.0


def load(path: Path):
    by_source: dict[str, int] = {}
    events: dict[int, list[dict]] = defaultdict(list)
    meta: dict[int, dict] = {}
    bad = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                item = json.loads(line)
            except ValueError:
                bad += 1
                continue
            ev = item.get("ev")
            if not isinstance(ev, dict) or ev.get("v") != 1:
                continue
            src = item.get("src", "?")
            if ev.get("e") == "hello" and isinstance(ev.get("pid"), int):
                by_source[src] = ev["pid"]
                meta.setdefault(ev["pid"], ev)
            pid = by_source.get(src)
            if pid is None:
                continue
            events[pid].append(ev)
    for pid in events:
        events[pid].sort(key=lambda e: e.get("t", 0))
    return events, meta, bad


def r2_shots(pad: list[dict], threshold: int):
    """(inicio, fin) en us de cada pulsación de R2; fin=None si no se soltó."""
    shots, down_at = [], None
    for ev in pad:
        down = bool(ev.get("b", 0) & BTN_R2) or ev.get("r2", 0) >= threshold
        if down and down_at is None:
            down_at = ev["t"]
        elif not down and down_at is not None:
            shots.append((down_at, ev["t"]))
            down_at = None
    if down_at is not None:
        shots.append((down_at, None))
    return shots


def l2_spans(pad: list[dict], threshold: int):
    spans, down_at = [], None
    for ev in pad:
        down = bool(ev.get("b", 0) & BTN_L2) or ev.get("l2", 0) >= threshold
        if down and down_at is None:
            down_at = ev["t"]
        elif not down and down_at is not None:
            spans.append((down_at, ev["t"]))
            down_at = None
    if down_at is not None:
        spans.append((down_at, None))
    return spans


def rumble_pulses(rumble: list[dict]):
    """(inicio, fin, pico L, pico S) de cada tramo distinto de cero (los refrescos re:1 no cuentan)."""
    pulses, start, peak_l, peak_s = [], None, 0, 0
    for ev in rumble:
        if ev.get("re"):
            continue
        l, s = ev.get("l", 0), ev.get("s", 0)
        if (l or s) and start is None:
            start, peak_l, peak_s = ev["t"], l, s
        elif (l or s):
            peak_l, peak_s = max(peak_l, l), max(peak_s, s)
        elif start is not None:
            pulses.append((start, ev["t"], peak_l, peak_s))
            start = None
    if start is not None:
        pulses.append((start, None, peak_l, peak_s))
    return pulses


def audio_onsets(audio: list[dict], port: int, rise_db: float, floor_db: float, refractory_us: int = 60000):
    series = [(ev["t"], db(ev.get("rms", 0))) for ev in audio if ev.get("p") == port]
    onsets, history, last = [], [], -10**18
    for t, level in series:
        history = [(ht, hl) for ht, hl in history if t - ht <= 300000]
        if history:
            base = statistics.median(hl for _, hl in history)
            if level >= floor_db and level - base >= rise_db and t - last >= refractory_us:
                onsets.append((t, level, level - base))
                last = t
        history.append((t, level))
    return series, onsets


def held_at(spans, t: int, after_us: int) -> bool:
    for start, end in spans:
        if start <= t and (end is None or t <= end + after_us):
            return True
    return False


def nearest(times: list[int], t: int):
    i = bisect.bisect_left(times, t)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(times) and (best is None or abs(times[j] - t) < abs(best - t)):
            best = times[j]
    return best


def describe(values: list[float]) -> str:
    if not values:
        return "n/d"
    values = sorted(values)
    return (f"mediana {statistics.median(values):.1f} ms, p10 {values[int(len(values) * 0.1)]:.1f}, "
            f"p90 {values[min(len(values) - 1, int(len(values) * 0.9))]:.1f} (n={len(values)})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Correlación R2 / rumble / audio de una grabación UNDS.")
    ap.add_argument("recording", type=Path, nargs="?",
                    help="archivo .jsonl (por defecto, el más reciente de .\\recordings)")
    ap.add_argument("--pid", type=int, help="emisor a analizar (por defecto el de más eventos)")
    ap.add_argument("--from", dest="t_from", type=float, default=None, help="segundos desde el primer evento del emisor")
    ap.add_argument("--to", dest="t_to", type=float, default=None)
    ap.add_argument("--r2-threshold", type=int, default=128)
    ap.add_argument("--after-release", type=float, default=300.0, help="ms tras soltar R2 que aún cuentan")
    ap.add_argument("--audio-port", type=int, default=0)
    ap.add_argument("--audio-rise", type=float, default=6.0, help="dB sobre la mediana de 300 ms")
    ap.add_argument("--audio-floor", type=float, default=-40.0, help="dBFS mínimos de un pico")
    ap.add_argument("--audio-window", type=float, default=150.0, help="ms máximos entre pulso de rumble y pico de audio")
    ap.add_argument("--plot", type=Path, help="PNG con la línea de tiempo (requiere matplotlib)")
    args = ap.parse_args(argv)

    if args.recording is None:
        folders = [Path("recordings"), Path(__file__).resolve().parent / "recordings"]
        found = sorted((f for d in folders if d.is_dir() for f in d.glob("unds_*.jsonl")),
                       key=lambda f: f.stat().st_mtime)
        if not found:
            print("No hay grabaciones en .\\recordings; pasa la ruta del .jsonl.")
            return 1
        args.recording = found[-1]
    if not args.recording.is_file():
        print(f"No existe {args.recording}. Grabaciones disponibles:")
        for f in sorted(args.recording.parent.glob("unds_*.jsonl")) if args.recording.parent.is_dir() else []:
            print(f"  {f}")
        return 1
    events, meta, bad = load(args.recording)
    if not events:
        print("La grabación no tiene eventos atribuibles a un emisor (¿falta el hello?).")
        return 1
    print(f"Emisores en {args.recording.name}:")
    for pid, evs in events.items():
        m = meta.get(pid, {})
        dur = (evs[-1]["t"] - evs[0]["t"]) / 1e6 if len(evs) > 1 else 0.0
        kinds = Counter(e.get("e") for e in evs)
        print(f"  pid {pid}: {m.get('exe', '?')} ({m.get('bin', '?')}) {len(evs)} eventos en {dur:.1f} s  "
              + " ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
    if bad:
        print(f"  ({bad} líneas no JSON ignoradas)")
    pid = args.pid if args.pid is not None else max(events, key=lambda p: len(events[p]))
    if pid not in events:
        print(f"No hay eventos del pid {pid}.")
        return 1
    evs = events[pid]
    t0 = evs[0]["t"]
    lo = t0 + int(args.t_from * 1e6) if args.t_from is not None else -10**18
    hi = t0 + int(args.t_to * 1e6) if args.t_to is not None else 10**18
    evs = [e for e in evs if lo <= e["t"] <= hi]
    pad = [e for e in evs if e.get("e") == "pad"]
    rumble = [e for e in evs if e.get("e") == "rumble"]
    audio = [e for e in evs if e.get("e") == "audio"]
    print(f"\nAnalizando pid {pid} ({meta.get(pid, {}).get('exe', '?')}): {len(pad)} pad, {len(rumble)} rumble, "
          f"{len(audio)} audio")

    shots = r2_shots(pad, args.r2_threshold)
    aims = l2_spans(pad, args.r2_threshold)
    pulses = rumble_pulses(rumble)
    series, onsets = audio_onsets(audio, args.audio_port, args.audio_rise, args.audio_floor)
    onset_times = [t for t, _, _ in onsets]
    after_us = int(args.after_release * 1000)

    print(f"\nR2: {len(shots)} pulsaciones; L2: {len(aims)} tramos apuntando")
    print(f"Rumble: {len(pulses)} pulsos (de 0 a !=0)")
    if pulses:
        durations = [(e - s) / 1000 for s, e, _, _ in pulses if e is not None]
        print(f"  duración: {describe(durations)}")
        print(f"  pico L (motor grande): {Counter(p[2] for p in pulses).most_common(5)}")
        print(f"  pico S (motor pequeño): {Counter(p[3] for p in pulses).most_common(5)}")
    print(f"Audio puerto {args.audio_port}: {len(series)} ventanas, {len(onsets)} picos "
          f"(subida >= {args.audio_rise} dB, suelo {args.audio_floor} dBFS)")

    with_r2 = [p for p in pulses if held_at(shots, p[0], after_us)]
    first_after_press = []
    pulse_starts = [p[0] for p in pulses]
    for start, _ in shots:
        i = bisect.bisect_left(pulse_starts, start)
        if i < len(pulse_starts) and pulse_starts[i] - start <= 500000:
            first_after_press.append((pulse_starts[i] - start) / 1000)
    audio_dt = []
    for start, *_ in pulses:
        near = nearest(onset_times, start)
        if near is not None and abs(near - start) <= args.audio_window * 1000:
            audio_dt.append((near - start) / 1000)

    print("\nCorrelación")
    if pulses:
        print(f"  pulsos de rumble con R2 pulsado (o <= {args.after_release:.0f} ms tras soltarlo): "
              f"{len(with_r2)}/{len(pulses)} ({100 * len(with_r2) / len(pulses):.0f}%)")
        print(f"  pulsos de rumble con pico de audio a <= {args.audio_window:.0f} ms: "
              f"{len(audio_dt)}/{len(pulses)} ({100 * len(audio_dt) / len(pulses):.0f}%)")
    print(f"  R2 pulsado -> primer pulso de rumble (<= 500 ms): {describe(first_after_press)}")
    print(f"  pico de audio - inicio del rumble: {describe(audio_dt)}  (positivo = el audio llega después)")

    ok = (len(pulses) >= 5 and len(with_r2) >= 0.8 * len(pulses) and len(audio_dt) >= 0.6 * len(pulses))
    print("\nVeredicto (heurístico): " + ("LISTO — el rumble sigue a R2 y coincide con picos de audio en main."
                                         if ok else
                                         "NO CONCLUYENTE — revisa la gráfica (--plot) y los umbrales; "
                                         "hace falta >= 5 pulsos, >= 80 % con R2 y >= 60 % con pico de audio."))

    if args.plot:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            print("Para --plot: python -m pip install matplotlib")
            return 0 if ok else 3
        sec = lambda t: (t - t0) / 1e6  # noqa: E731
        fig, axes = plt.subplots(3, 1, sharex=True, figsize=(14, 7), constrained_layout=True)
        axes[0].step([sec(e["t"]) for e in pad], [e.get("r2", 0) for e in pad], where="post", label="r2")
        axes[0].step([sec(e["t"]) for e in pad], [e.get("l2", 0) for e in pad], where="post", label="l2", alpha=0.6)
        axes[0].set_ylabel("gatillos"); axes[0].legend(loc="upper right")
        rs = [e for e in rumble if not e.get("re")]
        axes[1].step([sec(e["t"]) for e in rs], [e.get("l", 0) for e in rs], where="post", label="L (grande)")
        axes[1].step([sec(e["t"]) for e in rs], [e.get("s", 0) for e in rs], where="post", label="S (pequeño)", alpha=0.7)
        axes[1].set_ylabel("rumble"); axes[1].legend(loc="upper right")
        axes[2].plot([sec(t) for t, _ in series], [max(lv, -80) for _, lv in series], lw=0.8, label=f"RMS puerto {args.audio_port}")
        lf = [(e["t"], db(e.get("lf", 0))) for e in audio if e.get("p") == args.audio_port]
        axes[2].plot([sec(t) for t, _ in lf], [max(lv, -80) for _, lv in lf], lw=0.8, alpha=0.7, label="LF <150 Hz")
        axes[2].scatter([sec(t) for t in onset_times], [lv for _, lv, _ in onsets], marker="v", color="red", s=18, label="picos")
        axes[2].set_ylabel("dBFS"); axes[2].set_xlabel("s"); axes[2].legend(loc="upper right")
        for ax in axes:
            for start, *_ in pulses:
                ax.axvline(sec(start), color="gray", lw=0.4, alpha=0.5)
        fig.savefig(args.plot, dpi=110)
        print(f"Gráfica: {args.plot.resolve()}")
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
