"""UNDS Studio: .ini con comentarios, validación, efectos/voces para dibujar, galería de tiro y HTTP."""
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "companion"))
import unds_studio as S  # noqa: E402


def studio_in_tmp():
    tmp = Path(tempfile.mkdtemp())
    for name in ("unds_triggers.ini", "unds_haptics.ini"):
        shutil.copy(ROOT / "companion" / name, tmp / name)
    st = S.Studio(tmp, tmp / "unds_triggers.ini", tmp / "unds_haptics.ini", demo=True, haptics=False, say=lambda s: None)
    return tmp, st


class IniDocTests(unittest.TestCase):
    def test_roundtrip_keeps_everything(self):
        for name in ("unds_triggers.ini", "unds_haptics.ini", "unds_launcher.ini"):
            text = (ROOT / "companion" / name).read_text(encoding="utf-8")
            self.assertEqual(S.IniDoc(text).text().rstrip("\n"), text.rstrip("\n"), name)

    def test_set_keeps_comment_and_column(self):
        doc = S.IniDoc("[default]\nr2_idle = WEAPON 3 4 5         ; R2 sin apuntar\nkick = OFF\n")
        doc.set("default", "r2_idle", "WEAPON 3 4 6")
        self.assertEqual(doc.lines[1], "r2_idle = WEAPON 3 4 6         ; R2 sin apuntar")
        doc.set("default", "r2_idle", "FEEDBACK 1 8 @90 > VIBRATION 1 8 20 @80")
        self.assertTrue(doc.lines[1].endswith(" ; R2 sin apuntar"))
        self.assertEqual(doc.get("default", "r2_idle"), "FEEDBACK 1 8 @90 > VIBRATION 1 8 20 @80")

    def test_add_key_section_and_unset(self):
        doc = S.IniDoc("[default]\na = 1\n; nota\n\n[arma 0x1]\nclase = pistola\n")
        doc.set("clase escopeta", "kick", "OFF @1")
        self.assertEqual(doc.get("clase escopeta", "kick"), "OFF @1")
        doc.set("default", "b", "2")
        self.assertEqual(doc.lines[:3], ["[default]", "a = 1", "b = 2"])
        doc.unset("clase escopeta", "kick")
        self.assertNotIn("[clase escopeta]", doc.text())
        self.assertEqual(doc.get("arma 0x1", "clase"), "pistola")

    def test_sections_for_advanced_editor(self):
        secs = {s["name"]: s for s in S.IniDoc((ROOT / "companion" / "unds_triggers.ini").read_text(encoding="utf-8")).sections()}
        keys = {k["key"]: k for k in secs["default"]["keys"]}
        self.assertEqual(keys["r2_idle"]["value"], "WEAPON 3 4 5")
        self.assertIn("R2 sin apuntar", keys["r2_idle"]["doc"])
        self.assertIn("arma 0x58d20069", secs)


class DrawingTests(unittest.TestCase):
    def test_effect_zones(self):
        z = S.effect_zones("WEAPON 3 4 8")
        self.assertEqual([c["f"] for c in z["zones"]], [0, 0, 0, 8, 0, 0, 0, 0, 0, 0])
        self.assertEqual(z["break"], 4)
        z = S.effect_zones("vib 2 5 30")
        self.assertEqual([c["v"] for c in z["zones"]][:3], [0, 0, 5])
        self.assertEqual(z["freq"], 30)
        with self.assertRaises(ValueError):
            S.effect_zones("FEEDBACK 1 9")

    def test_kick_and_voice(self):
        self.assertEqual(S.kick_phases("FEEDBACK 1 8 @55 > VIBRATION 1 8 40 @25", 70),
                         [{"effect": "FEEDBACK 1 8", "ms": 55}, {"effect": "VIBRATION 1 8 40", "ms": 25}])
        w = S.voice_wave("SINE 65 80 0.55 0.2 30 + NOISE 300 25 0.25 0.2 6")
        self.assertEqual(w["ms"], 80)
        self.assertTrue(0 < w["peak"] < 1.0)
        self.assertGreater(max(w["r"]), max(w["l"]))          # pan 0,2: más a la derecha
        self.assertEqual(S.voice_wave("OFF")["ms"], 0)


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.st = studio_in_tmp()

    def test_set_validates_and_writes(self):
        bad = self.st.set_value("triggers", "clase escopeta", "kick", "FEEDBACK 1 9 @90")
        self.assertFalse(bad["ok"])
        self.assertIn("fuera de 1-8", bad["error"])
        self.assertIsNone(S.IniDoc.read(self.tmp / "unds_triggers.ini").get("clase escopeta", "kick"))
        ok = self.st.set_value("triggers", "clase escopeta", "kick", "FEEDBACK 1 8 @90")
        self.assertTrue(ok["ok"])
        self.assertEqual(self.st.trigger_values("escopeta")["kick"], {"value": "FEEDBACK 1 8 @90", "origin": "clase"})
        self.st.set_value("triggers", "clase escopeta", "kick", None)
        self.assertEqual(self.st.trigger_values("escopeta")["kick"]["origin"], "clase-código")
        self.assertTrue(self.st.set_value("haptics", "clase escopeta", "fire", "SINE 45 220 1.0 0 90")["ok"])
        self.assertEqual(self.st.voice_values("escopeta")["fire"]["origin"], "clase")
        self.assertFalse(self.st.set_value("haptics", "default", "hit_pan", "7")["ok"])

    def test_restore(self):
        original = (self.tmp / "unds_triggers.ini").read_text(encoding="utf-8")
        self.st.set_value("triggers", "default", "r2_idle", "WEAPON 3 4 6")
        self.st.restore()
        self.assertEqual((self.tmp / "unds_triggers.ini").read_text(encoding="utf-8"), original)

    def test_range_semi_and_auto(self):
        st = self.st
        st.range_on, st.pad.demo = True, False                 # pad manual para el test
        st.set_range({"cls": "pistola"})
        now = 100.0
        st.pad.l2, st.pad.r2 = 0, 0
        st.desired(now)
        st.pad.r2 = 200
        want = st.desired(now + 0.01)
        self.assertEqual((st.ammo, st.shots), (7, 1))
        self.assertEqual(str(want["R"]), "VIBRATION 1 4 40")     # kick de [default]
        self.assertEqual(str(st.desired(now + 0.2)["R"]), "WEAPON 3 4 5")   # vuelve al tope sin apuntar
        st.pad.l2 = 200
        self.assertEqual(str(st.desired(now + 0.3)["R"]), "WEAPON 3 4 8")   # apuntando
        st.set_range({"cls": "automatica"})
        st.pad.r2 = 0
        st.desired(now + 1.0)
        st.pad.r2 = 255
        t = now + 1.01
        for _ in range(60):                                    # 0,6 s con R2 a fondo
            st.desired(t)
            t += 0.01
        self.assertGreaterEqual(st.shots, 1 + 5)               # 1 de la pistola + ~6 balas a 106 ms
        self.assertEqual(st.ammo, 30 - (st.shots - 1))

    def test_empty_and_reload(self):
        st = self.st
        st.pad.demo = False
        st.set_range({"cls": "lanzagranadas"})
        st.pad.r2 = 0
        st.desired(1.0)
        st.pad.r2 = 255
        st.desired(1.01)
        self.assertEqual(st.ammo, 0)
        st.pad.r2 = 0
        self.assertEqual(str(st.desired(3.0)["R"]), "OFF")      # empty_r2
        st.pad.buttons = 0x10                                  # □
        st.desired(3.01)
        st.pad.buttons = 0
        st.desired(3.02 + S.RELOAD_S)
        self.assertEqual(st.ammo, 1)

    def test_test_effects_override(self):
        st = self.st
        st.pad.demo = False
        self.assertTrue(st.test({"kind": "trigger", "side": "L", "text": "VIBRATION 1 8 30", "ms": 1000})["ok"])
        self.assertEqual(str(st.desired(time.monotonic())["L"]), "VIBRATION 1 8 30")
        self.assertFalse(st.test({"kind": "trigger", "side": "R", "text": "NADA"})["ok"])
        self.assertFalse(st.test({"kind": "voice", "text": "SINE 60 100 1 0 10"})["ok"])   # sin hápticos
        st.test({"kind": "release"})
        self.assertNotEqual(str(st.desired(time.monotonic())["L"]), "VIBRATION 1 8 30")


class HttpTests(unittest.TestCase):
    def test_api(self):
        tmp, st = studio_in_tmp()
        server = S.ThreadingHTTPServer(("127.0.0.1", 0), S.make_handler(st))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            html = urllib.request.urlopen(base + "/").read().decode("utf-8")
            self.assertIn("UNDS Studio", html)
            model = json.load(urllib.request.urlopen(base + "/api/model"))
            self.assertIn("escopeta", model["classValues"])
            self.assertIn("WEAPON", model["modes"])
            req = urllib.request.Request(base + "/api/set", data=json.dumps(
                {"file": "triggers", "section": "clase subfusil", "key": "r2_aim", "value": "WEAPON 3 4 7"}).encode(),
                headers={"Content-Type": "application/json"})
            self.assertTrue(json.load(urllib.request.urlopen(req))["ok"])
            live = json.load(urllib.request.urlopen(base + "/api/live"))
            self.assertIn("range", live)
            eff = json.load(urllib.request.urlopen(base + "/api/effect?text=FEEDBACK%201%209"))
            self.assertFalse(eff["ok"])
            bad = urllib.request.Request(base + "/api/live", headers={"Host": "evil.example"})
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(bad)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
