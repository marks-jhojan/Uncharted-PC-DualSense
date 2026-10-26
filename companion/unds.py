#!/usr/bin/env python3
"""UNDS — companion completo: gatillos y lightbar (por el port, SDL) + hápticos (WASAPI).

DSX debe estar CERRADO: abierto activa el rumble de compatibilidad del mando y eso apaga los
hápticos (verificado). Gatillos y lightbar van por el canal fx del port (unds_port.py, parche 0004)
por defecto; --backend hid vuelve al HID directo (unds_hid.py, necesita hidapi).

Un solo proceso escucha el bus del port (UDP 47111) y alimenta a la vez:
  - unds_triggers: gatillos adaptativos y lightbar (unds_triggers.ini, [output] backend = port | hid | dsx, o --backend);
  - unds_haptics:  hápticos por los canales 3-4 del DualSense USB (unds_haptics.ini):
      rumble del juego -> hápticos, voz "fire" en cada disparo propio (misma detección que el kick
      del gatillo: evento "shot" del estado del juego si el port lo publica, si no la firma de
      rumble), voz "hit" en cada daño recibido, repartida entre los actuadores según el lado del que
      viene (evento dmg, Día 6) y con el rumble de daño del juego atenuado, voz "death" con el rumble
      del juego en silencio hasta reaparecer y, opcional, audio -> hápticos.
Los dos .ini se recargan al guardarlos. Graba el JSONL como unds_listen.py, más las líneas
"src":"hap" (voces con su latencia evento -> DAC estimada).

    python unds.py -v                 # todo
    python unds.py --no-triggers      # solo hápticos (no toca gatillos ni lightbar)
    python unds.py --no-haptics       # = unds_triggers.py

Launcher (Environment):  UNDS_EVENTS=127.0.0.1:47111 UNDS_TRIGGER=30,95 UNDS_RUMBLE=0
(UNDS_LED=0 ya no hace falta con el canal fx: el port deja la lightbar al companion mientras la lleva
y se la devuelve al juego al cerrarlo; puede seguir puesto.)
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from unds_haptics import HapticConfig, HapticError, HapticOutput, hit_mix  # noqa: E402
from unds_hid import dsx_running  # noqa: E402
from unds_listen import Emitter, damage_azimuth  # noqa: E402
from unds_triggers import TriggerCompanion  # noqa: E402


class UndsCompanion(TriggerCompanion):
    def __init__(self, args: argparse.Namespace):
        super().__init__(args)
        self.hap_config = HapticConfig(Path(args.haptics_config))
        self.hap_config.load()                       # ValueError -> main lo informa
        self.haptics: HapticOutput | None = None
        if not args.no_haptics:
            self.haptics = HapticOutput(self.hap_config.device, say=self.say)
            self.haptics.set_profile(self.hap_config.profile("default"))
        self.engine.on_event = self.engine_event
        self.hap_pid: int | None = None              # emisor al que siguen los hápticos
        self.hap_checked = 0.0
        self.hap_stats_at = time.perf_counter()
        self.dsx_checked = 0.0
        self.dsx_open: bool | None = None

    # ---- puente con el motor de gatillos ---------------------------------------------------------
    def engine_event(self, name: str) -> None:
        if not self.haptics:
            return
        if name == "death":
            self.haptics.mute_rumble(True)       # la voz de muerte manda (death_duck)
            self.haptics.voice("death")
        elif name == "shot":                     # cada disparo de una ráfaga automática
            self.haptics.voice("fire_auto")
        elif name == "mount_shot":               # Día 8b: cada disparo de la torreta montada
            self.mounted_voice("mount_r2")
        elif name == "respawn":
            self.haptics.mute_rumble(False)
            p = self.haptics.profile
            if p is not None and p.death_stop_s > 0:
                self.haptics.stop_voice("death", p.death_stop_s)

    def rumble_hook(self, em: Emitter, ev: dict, kind: str | None) -> None:
        if not self.haptics:
            return
        rx = time.perf_counter()
        large, small = int(ev.get("l", 0)), int(ev.get("s", 0))
        # Solo se silencia (fire_duck_ms) el rumble con forma de disparo o su cola: si a la vez hay
        # una explosión (motor grande por encima de fire_l_max), sigue sonando.
        p = self.config.profile(self.game)
        self.haptics.rumble(large, small, duckable=large <= p.fire_l_max)
        if kind == "fire":
            self.haptics.voice("fire", rx)
        elif kind == "fire_auto":
            self.haptics.voice("fire_auto", rx)
        elif kind in ("mount_r2", "mount_r2_single", "mount_r2_light", "mount_l2"):
            self.mounted_voice(kind, rx)
        elif kind == "hit":
            # Sin estado del juego (U2/U3) el daño solo se conoce por la firma de rumble: sin dirección.
            # Con hit_source = state (por defecto) esa voz no suena: el rumble de daño ya se traduce.
            hp = self.haptics.profile
            if hp is not None and hp.hit_source == "any":
                self.haptics.voice("hit", rx)

    def mounted_voice(self, kind: str, rx: float | None = None) -> None:
        """Día 8b: voz de la clase del arma montada (mounted_r2_class / mounted_l2_class), como un disparo
        de esa clase; si la clase no tiene voz propia, fire_auto (R2) o fire (L2)."""
        if not self.haptics:
            return
        p = self.config.profile(self.game)
        cls, base = {"mount_r2": (p.mounted_r2_class, "fire_auto"),
                     "mount_r2_single": (p.mounted_r2_single_class, "fire"),
                     "mount_r2_light": (p.mounted_r2_light_class, "fire")}.get(kind, (p.mounted_l2_class, "fire"))
        bank = self.haptics.bank
        for name in (f"{base}@clase:{cls}", base):
            if name in bank:
                self.haptics.voice(name, rx if rx is not None else time.perf_counter())
                return

    def shot_hook(self, em: Emitter, ev: dict, kind: str | None) -> None:
        """Disparo exacto del estado del juego (Día 5). Día 7: voz del arma ([arma 0x...]) o de su clase
        si existe (aunque sea OFF, que entonces no suena); si no, fire / fire_auto del perfil."""
        if not (self.haptics and kind in ("fire", "fire_auto")):
            return
        wid = str(ev.get("w", "0x0"))
        with self.engine.cond:
            cls = self.engine.weapon_class(wid)
        bank = self.haptics.bank
        for name in (f"{kind}@arma:{wid}", f"{kind}@clase:{cls}", kind):
            if name in bank:
                self.haptics.voice(name, time.perf_counter())
                break

    def damage_hook(self, em: Emitter, ev: dict, kind: str | None) -> None:
        """dmg del estado del juego: golpe direccional (lado de la fuente por paneo, delante/detrás por
        timbre: agudo / grave) con fuerza según el daño."""
        hp = self.haptics.profile if self.haptics else None
        if hp is None or kind != "hit":
            return
        rx = time.perf_counter()
        azimuth = damage_azimuth(ev)
        amount = int(ev["amt"]) if isinstance(ev.get("amt"), (int, float)) else None
        bank = self.haptics.bank
        mix = hit_mix(hp, azimuth, amount, lambda name: bank.get(name) is not None)
        for name, gains in mix:
            self.haptics.voice(name, rx, gains)
        if self.args.verbose:
            where = "sin fuente" if azimuth is None else f"{azimuth:+.0f}°"
            text = " + ".join(f"{n.replace('hit_', '')} {g[0]:.2f}/{g[1]:.2f}" for n, g in mix)
            self.say(f"HIT   daño {amount if amount is not None else '?'} desde {where}: {text} (izq/der)")

    # ---- eventos del port ------------------------------------------------------------------------
    def on_hello(self, em: Emitter, ev: dict) -> None:
        super().on_hello(em, ev)
        if self.haptics and self.active_pid == em.pid and self.hap_pid != em.pid:
            self.hap_pid = em.pid
            self.haptics.silence()
            self.haptics.set_profile(self.hap_config.profile(self.game))
            self.say(f"HAPT  juego {self.game} (pid {em.pid}): perfil "
                     f"[{self.game if self.game in self.hap_config.profiles else 'default'}]")

    def on_bye(self, em: Emitter, ev: dict) -> None:
        super().on_bye(em, ev)
        self.hap_detach_if_needed()

    def on_audio(self, em: Emitter, ev: dict) -> None:
        super().on_audio(em, ev)
        if self.haptics and em.pid == self.active_pid and ev.get("ty") == 0:
            self.haptics.audio_level(int(ev.get("lf", 0)), int(ev.get("n", 512)))

    def hap_detach_if_needed(self) -> None:
        if self.haptics and self.active_pid is None and self.hap_pid is not None:
            self.hap_pid = None
            self.haptics.silence()

    # ---- bucle -----------------------------------------------------------------------------------
    def check_dsx(self, now: float) -> None:
        """DSX abierto apaga los hápticos (rumble de compatibilidad) y pelea por los gatillos."""
        if now - self.dsx_checked < 15.0 and self.dsx_open is not None:
            return
        self.dsx_checked = now
        running = dsx_running()
        if running and not self.dsx_open:
            self.say("AVISO DSX está abierto: apaga los hápticos del mando y pelea por los gatillos. Ciérralo "
                     "(también desde la bandeja del sistema).")
        elif self.dsx_open and not running:
            self.say("DSX cerrado: los hápticos vuelven en el próximo reporte de gatillos/lightbar.")
        self.dsx_open = running

    def periodic(self) -> None:
        super().periodic()
        self.check_dsx(time.perf_counter())
        self.hap_detach_if_needed()
        if not self.haptics:
            return
        now = time.perf_counter()
        for item in self.haptics.drain_reports():
            self.write({"rx_us": time.perf_counter_ns() // 1000, "wall": round(time.time(), 6), "src": "hap",
                        "hap": item})
            if self.args.verbose:
                self.say(f"HAPT  voz {item['voz']}: evento -> DAC ~{item['e2dac_ms']:.1f} ms "
                         f"(cola {item['cola_ms']:.1f} ms)")
        if now - self.hap_checked >= 1.0:
            self.hap_checked = now
            if self.hap_config.changed():
                try:
                    self.hap_config.load()
                    reopen = self.haptics.set_device_config(self.hap_config.device)
                    self.haptics.set_profile(self.hap_config.profile(self.game))
                    self.say(f"HAPT  config recargada de {self.hap_config.path}")
                    if reopen:
                        self.haptics.reopen()
                except ValueError as error:
                    self.say(f"HAPT  config NO recargada ({error}); sigo con la anterior")
                    self.hap_config.mtime = self.hap_config.path.stat().st_mtime
        if self.args.stats_every > 0 and now - self.hap_stats_at >= self.args.stats_every:
            self.hap_stats_at = now
            self.say(self.haptics.stats_text())

    def summary(self) -> None:
        super().summary()
        if self.haptics:
            print(self.haptics.stats_text().replace("HAPT  ", "Hápticos: ", 1))
            print(f"     dispositivo: {self.haptics.device_name}  aperturas={self.haptics.counts['aperturas']} "
                  f"bloques={self.haptics.counts['bloques']}")

    def run(self) -> int:
        if self.haptics:
            self.haptics.start()
        try:
            return super().run()
        finally:
            if self.haptics:
                self.haptics.close()


def build_parser(here: Path | None = None) -> argparse.ArgumentParser:
    here = here or Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="UNDS: gatillos y lightbar (port/SDL) + hápticos (WASAPI).")
    parser.add_argument("--host", default="127.0.0.1", help="dirección donde escuchar el port (127.0.0.1)")
    parser.add_argument("--port", type=int, default=47111, help="puerto UDP del bus UNDS (47111)")
    parser.add_argument("--config", default=str(here / "unds_triggers.ini"), help="gatillos (unds_triggers.ini)")
    parser.add_argument("--haptics-config", default=str(here / "unds_haptics.ini"), help="hápticos (unds_haptics.ini)")
    parser.add_argument("--out", default="recordings", help="carpeta de grabaciones (recordings)")
    parser.add_argument("--no-record", action="store_true", help="no grabar JSONL")
    parser.add_argument("--show", default="hello,bye", help="como unds_listen.py (hello,bye; añade gs,shot,dmg para ver el estado)")
    parser.add_argument("--stats-every", type=float, default=10.0, help="segundos entre líneas STATS (10)")
    parser.add_argument("--timeout", type=float, default=3.0, help="segundos de silencio = juego cerrado (3)")
    parser.add_argument("--calibrate", action="store_true", help="calibración de R2 (ver unds_triggers.py)")
    parser.add_argument("--no-triggers", "--no-dsx", dest="no_dsx", action="store_true",
                        help="no tocar gatillos ni lightbar (solo hápticos)")
    parser.add_argument("--no-haptics", action="store_true", help="sin hápticos (solo gatillos y lightbar)")
    parser.add_argument("--backend", choices=("port", "hid", "dsx"),
                        help="gatillos y lightbar: port | hid | dsx (gana a [output] backend del .ini)")
    parser.add_argument("--verbose", "-v", action="store_true", help="imprime cada disparo, voz y paquete")
    parser.add_argument("--quiet-kick", action="store_true", help="no imprimir cambios de R2")
    return parser


def setup_console() -> None:
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    sys.setswitchinterval(0.001)   # el callback de audio espera menos al GIL (por defecto 5 ms)


def create_companion(args: argparse.Namespace) -> UndsCompanion | None:
    """El companion listo para run(); None (con el motivo en stderr) si la configuración o el audio fallan."""
    args.dry_run = args.no_dsx                        # lo que espera el motor de gatillos
    try:
        return UndsCompanion(args)
    except ValueError as error:
        print(f"error de configuración: {error}", file=sys.stderr)
    except HapticError as error:
        print(f"hápticos: {error}", file=sys.stderr)
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_console()
    companion = create_companion(args)
    if companion is None:
        return 2
    signal.signal(signal.SIGINT, signal.default_int_handler)
    return companion.run()


if __name__ == "__main__":
    sys.exit(main())
