"""Día 9: la lightbar roja parpadeante del lanzador pasa por encima del color del juego y luego vuelve."""
import argparse
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "companion"))
import unds_triggers as T  # noqa: E402


class AlertTest(unittest.TestCase):
    def test_blink_and_back(self):
        cfg = T.Config(Path(__file__).resolve().parent.parent / "companion" / "unds_triggers.ini")
        cfg.load()
        args = argparse.Namespace(dry_run=True, calibrate=False, verbose=False, quiet_kick=True, no_dsx=True)
        e = T.Engine(cfg, args, lambda s: None)
        with e.cond:
            e.attach("u1")
        e.led((0, 200, 40))
        with e.cond:
            base = e.desired(time.perf_counter())["RGB"]
        e.alert(1.0)
        t0 = e.alert_t
        with e.cond:
            self.assertEqual(e.desired(t0 + 0.01)["RGB"], (255, 0, 0, 255))
            self.assertEqual(e.desired(t0 + 0.30)["RGB"], (0, 0, 0, 255))
            self.assertEqual(e.desired(t0 + 0.55)["RGB"], (255, 0, 0, 255))
            self.assertEqual(e.desired(t0 + 1.20)["RGB"], base)
            wait = e.next_deadline(t0 + 0.10)
        self.assertTrue(0.14 < wait <= 0.152, wait)


if __name__ == "__main__":
    unittest.main()
