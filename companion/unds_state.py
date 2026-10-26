#!/usr/bin/env python3
"""UNDS — Día 5: análisis del estado del juego (eventos gs / shot / dmg del parche 0004) en una grabación.

Comprueba, con datos, lo que el parche afirma por análisis estático y lo que aún es [HIPÓTESIS]:
  1. hooks: si el port los instaló y se llamaron (hello: gsp/gsw/gss/gsd/gsf).
  2. vida: línea temporal (daño, regeneración, muerte = 0, reaparición = 100 %) y su relación con la
     lightbar de muerte del juego (64,0,0).
  3. disparos: shot por arma (id), intervalo entre disparos (cadencia), y cada shot frente a la firma
     de rumble del Día 4 (adelanto/retraso shot -> rumble).
  4. munición: recargas (el cargador sube), cambios de arma, cargador vacío.
  5. daño: cantidad, tipo, ángulo de la fuente [HIPÓTESIS: signo y que +0x10 sea la posición].
  6. ups = 0 en los refrescos (el juego no actualiza al jugador) [HIPÓTESIS: cinemáticas/cargas].

    python unds_state.py                                  # última grabación de .\\recordings\\
    python unds_state.py recordings\\unds_20261023_200000.jsonl --hp     # con la vida paso a paso

Solo biblioteca estándar.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from unds_listen import damage_azimuth  # noqa: E402

FIRE_SIG = lambda l, s: s >= 200 and 76 <= l <= 120  # noqa: E731  (firma de disparo del Día 4)


def load(path: Path) -> list[tuple[float, dict]]:
    """(segundos desde el primer evento, evento) del emisor real, ordenados por t del port."""
    out = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            ev = item.get("ev")
            if isinstance(ev, dict) and isinstance(ev.get("t"), int) and item.get("src") not in ("dsx", "hap"):
                out.append(ev)
    out.sort(key=lambda e: e["t"])
    if not out:
        return []
    t0 = out[0]["t"]
    return [((e["t"] - t0) / 1e6, e) for e in out]


def fmt_t(t: float) -> str:
    return f"{t:8.2f} s"


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Análisis del estado del juego (gs/shot/dmg) de una grabación UNDS.")
    parser.add_argument("file", nargs="?", help="grabación JSONL (por defecto la última de recordings/)")
    parser.add_argument("--hp", action="store_true", help="imprimir cada cambio de vida")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    if args.file:
        path = Path(args.file)
    else:
        files = sorted((here / "recordings").glob("unds_*.jsonl"), key=lambda p: p.stat().st_mtime)
        path = files[-1] if files else None
    if path is None or not path.exists():
        print("no encuentro la grabación", file=sys.stderr)
        return 2
    events = load(path)
    print(f"{path.name}: {len(events)} eventos, {events[-1][0] if events else 0:.1f} s")
    kinds = Counter(e.get("e") for _, e in events)
    print("  por tipo: " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))

    # 1. hooks
    hellos = [(t, e) for t, e in events if e.get("e") == "hello"]
    if not hellos:
        print("sin hello: ¿grabación vacía?")
        return 1
    last = hellos[-1][1]
    print(f"\n1. HOOKS  port {last.get('ver')} exe={last.get('exe')} base={last.get('base')}")
    if "gsp" not in last:
        print("   el port no publica estado (sin parche 0004 o UNDS_STATE=0)")
        return 0
    print(f"   llamadas: jugador={last['gsp']} controlador de armas={last['gsw']} shot={last['gss']} "
          f"daño={last['gsd']} lecturas fallidas={last['gsf']}")
    if not last["gsp"]:
        print("   NINGUNA llamada al hook del jugador: la versión del juego no coincide con las firmas "
              "(mira 'Callbacks: UNDS ...' en last_run.log)")

    # 2. vida
    gs = [(t, e) for t, e in events if e.get("e") == "gs" and not e.get("re")]
    refresh = [(t, e) for t, e in events if e.get("e") == "gs" and e.get("re")]
    leds = [(t, (e.get("r"), e.get("g"), e.get("b"))) for t, e in events if e.get("e") == "led" and not e.get("re")]
    print(f"\n2. VIDA  ({len(gs)} cambios publicados, {len(refresh)} refrescos)")
    bad = [t for t, e in gs if e.get("hp", 0) < 0]
    if bad:
        print(f"   AVISO {len(bad)} valores que no pueden ser vida (hp=-1), el primero en {fmt_t(bad[0])}: "
              f"offset de vida equivocado para esta versión")
    prev = None
    deaths, respawns, minima = [], [], []
    low = None
    for t, e in gs:
        hp = e.get("hp", -1)
        if hp < 0:
            continue
        if prev is not None:
            if hp == 0 and prev > 0:
                deaths.append(t)
            if hp == 1000 and prev == 0:
                respawns.append(t)
            if hp < prev:
                low = hp if low is None else min(low, hp)
            if hp > prev and low is not None:
                minima.append(low)
                low = None
        if args.hp and hp != prev:
            print(f"   {fmt_t(t)} vida {hp / 10:5.1f}%{' herido' if e.get('inj') else ''}")
        prev = hp
    print(f"   muertes (vida -> 0): {len(deaths)} {[round(t, 2) for t in deaths]}")
    print(f"   reapariciones (0 -> 100 %): {len(respawns)} {[round(t, 2) for t in respawns]}")
    if minima:
        print(f"   mínimos entre regeneraciones: {', '.join(f'{m / 10:.0f}%' for m in minima[:20])}")
    red = [t for t, rgb in leds if rgb == (64, 0, 0)]
    for td in deaths:
        near = [tr - td for tr in red if abs(tr - td) < 5.0]
        if near:
            print(f"   muerte {fmt_t(td)}: lightbar de muerte {min(near, key=abs) * 1000:+.0f} ms respecto a vida 0")
        else:
            print(f"   muerte {fmt_t(td)}: sin lightbar (64,0,0) a ±5 s")
    regen = []
    for (t1, e1), (t2, e2) in zip(gs, gs[1:]):
        if 0 < e1.get("hp", -1) < e2.get("hp", -1) < 1000 and t2 - t1 < 0.5:
            regen.append((e2["hp"] - e1["hp"]) / 1000.0 / (t2 - t1))
    if regen:
        print(f"   regeneración: mediana {statistics.median(regen) * 100:.1f} %/s ({len(regen)} tramos)")

    # 3. disparos
    shots = [(t, e) for t, e in events if e.get("e") == "shot"]
    print(f"\n3. DISPAROS  {len(shots)} shot")
    by_weapon = defaultdict(list)
    for t, e in shots:
        by_weapon[e.get("w")].append((t, e))
    for w, items in by_weapon.items():
        gaps = [b[0] - a[0] for a, b in zip(items, items[1:]) if b[0] - a[0] < 1.0]
        cadence = f", intervalo mediano {statistics.median(gaps) * 1000:.0f} ms ({len(gaps)} pares < 1 s)" if gaps else ""
        caps = sorted({e.get("cap") for _, e in items})
        print(f"   arma {w}: {len(items)} disparos, cargador {caps}{cadence}")
    rumbles = [(t, e.get("l", 0), e.get("s", 0)) for t, e in events if e.get("e") == "rumble" and not e.get("re")]
    fire_edges = []
    prev_sig = False
    for t, l, s in rumbles:
        sig = FIRE_SIG(l, s)
        if sig and not prev_sig:
            fire_edges.append((t, l, s))
        prev_sig = sig
    if shots:
        # Solo el primer disparo de cada serie (nada en los 0,3 s anteriores): en las automáticas el
        # rumble es UN flanco sostenido por ráfaga.
        firsts = [t for i, (t, _) in enumerate(shots) if i == 0 or t - shots[i - 1][0] > 0.3]
        deltas = []
        unmatched = []
        for t in firsts:
            near = [ft - t for ft, _, _ in fire_edges if -0.15 <= ft - t <= 0.3]
            if near:
                deltas.append(min(near, key=abs))
            else:
                unmatched.append(t)
        edges_alone = [ft for ft, _, _ in fire_edges if not any(-0.3 <= ft - t <= 0.15 for t, _ in shots)]
        print(f"   firma de rumble del Día 4: {len(fire_edges)} flancos; series de disparos: {len(firsts)}, "
              f"con flanco cercano: {len(deltas)}, sin flanco: {len(unmatched)}; flancos sin shot: {len(edges_alone)}")
        if deltas:
            print(f"   rumble respecto al primer shot: mediana {statistics.median(deltas) * 1000:+.1f} ms "
                  f"(min {min(deltas) * 1000:+.1f}, max {max(deltas) * 1000:+.1f}); + = el shot llega antes")
        if edges_alone:
            print(f"   flancos de firma sin shot (¿disparos que el hook no ve o firmas falsas?): "
                  f"{[round(t, 2) for t in edges_alone[:15]]}")

    # 4. munición
    print("\n4. MUNICIÓN")
    prev = None
    reloads, switches, empties = [], [], []
    for t, e in gs:
        if prev is not None:
            if e.get("w") != prev.get("w") or e.get("ws") != prev.get("ws"):
                switches.append((t, prev.get("w"), e.get("w"), e.get("ws")))
            elif e.get("clip", -1) > prev.get("clip", -1) >= 0:
                reloads.append((t, e.get("w"), prev.get("clip"), e.get("clip"), e.get("res")))
            if e.get("clip") == 0 and prev.get("clip", 0) > 0 and e.get("cap", 0) > 0:
                empties.append((t, e.get("w")))
        prev = e
    print(f"   cambios de arma: {len(switches)}")
    for t, a, b, ws in switches[:15]:
        print(f"     {fmt_t(t)} {a} -> {b} (slot {ws})")
    print(f"   recargas: {len(reloads)}")
    for t, w, a, b, res in reloads[:15]:
        print(f"     {fmt_t(t)} arma {w}: {a} -> {b} (reserva {res})")
    print(f"   cargador vacío: {len(empties)} {[round(t, 2) for t, _ in empties][:15]}")
    grenades = sorted({(e.get("gr"), e.get("grr")) for _, e in gs if e.get("gr", -1) >= 0})
    print(f"   granadas vistas (gr, grr) [HIPÓTESIS: gr = granadas]: {grenades[:10]}")

    # 5. daño
    dmg = [(t, e) for t, e in events if e.get("e") == "dmg"]
    print(f"\n5. DAÑO  {len(dmg)} eventos")
    types = Counter(e.get("ty") for _, e in dmg)
    if types:
        print("   tipos: " + ", ".join(f"{k}={v}" for k, v in types.most_common()))
    for t, e in dmg[:40]:
        a = damage_azimuth(e)
        where = "sin fuente" if a is None else f"fuente a {a:+4.0f}°"
        dist = ""
        if e.get("src") and e.get("loc"):
            dx = (e.get("sx", 0) - e.get("px", 0)) / 1000
            dy = (e.get("sy", 0) - e.get("py", 0)) / 1000
            dz = (e.get("sz", 0) - e.get("pz", 0)) / 1000
            dist = f" a {(dx * dx + dy * dy + dz * dz) ** 0.5:5.1f} m"
        amount = "letal" if e.get("amt", 0) >= 1000 else f"-{e.get('amt', 0) / 10:4.1f}%"
        print(f"   {fmt_t(t)} tipo {e.get('ty')} {amount} (vida {e.get('hp', 0) / 10:5.1f}%) {where}{dist}")
    if len(dmg) > 40:
        print(f"   ... y {len(dmg) - 40} más")

    # 6. jugador sin actualizar
    print("\n6. JUGADOR SIN ACTUALIZAR (refrescos con ups=0)")
    spans, start = [], None
    for t, e in refresh:
        if e.get("ups") == 0 and start is None:
            start = t
        elif e.get("ups", 0) > 0 and start is not None:
            spans.append((start, t))
            start = None
    if start is not None:
        spans.append((start, refresh[-1][0]))
    if not spans:
        print("   ninguno")
    for a, b in spans[:20]:
        print(f"   {fmt_t(a)} -> {fmt_t(b)} ({b - a:.1f} s)")
    ups = [e.get("ups", 0) for _, e in refresh if e.get("ups", 0) > 0]
    if ups:
        print(f"   ups típico con jugador activo: {statistics.median(ups)} por 0,5 s "
              f"(= {statistics.median(ups) * 2:.0f} actualizaciones/s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
