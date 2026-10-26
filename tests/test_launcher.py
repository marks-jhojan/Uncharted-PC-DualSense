import sys, os, time, tempfile, unittest
from pathlib import Path
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'companion'))
import unds_launcher as L

GOOD = "Red zone: 1702 of 1702 guest functions keep data below rsp; 5762 memory accesses in them run with rsp 128 bytes lower (12 jumps, 3 moved neighbours), 46 left as they were; skipped: 4 with calls, 0\n"
BAD = "Red zone: 1702 of 1702 guest functions keep data below rsp; 2489 memory accesses in them run with rsp 128 bytes lower (12 jumps, 3 moved neighbours), 3319 left as they were; skipped: 4 with calls, 0\n"

class T(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp()); self.log = self.d/'last_run.log'; self.ev=[]
        self.w = L.LogWatcher(self.log, 300, lambda k,t: self.ev.append(k), started=time.time()-5)
    def write(self, s, mode='a'):
        with open(self.log, mode, encoding='utf-8') as f: f.write(s)
    def test_good_then_loss_assert_fault(self):
        self.write("hdr\n"+GOOD, 'w'); self.w.poll(); self.assertEqual(self.ev, ['ok'])
        self.write("Red zone: guest page fault 1 at ..., its data may be lost\n"); self.w.poll()
        self.write("Red zone: guest page fault 2 ..., its data may be lost\n"); self.w.poll()
        self.write("*** ASSERTION: iSubMesh < m_numSubMeshInstances\n"); self.w.poll()
        self.write("Fault: execute access to 0000000000000400 at 0000000000000400 (thread 1)\n"); self.w.poll()
        self.assertEqual(self.ev, ['ok','datos','asercion','fallo'])
    def test_partial_line(self):
        self.write("hdr\nRed zone: 1702 of 1702 guest functions keep data below rsp; 2489 memory acc", 'w'); self.w.poll()
        self.assertEqual(self.ev, [])
        self.write("esses in them run with rsp 128 bytes lower (1 jumps, 0 moved neighbours), 3319 left as they were; x\n"); self.w.poll(); self.assertEqual(self.ev, ['arranque'])
    def test_restart_shorter(self):
        self.write("A"*300+"\n"+BAD, 'w'); self.w.poll(); self.write("x"*1000+"\n"); self.w.poll()
        self.write("B\n"+BAD, 'w'); self.w.poll()
        self.assertEqual(self.ev, ['arranque','arranque'])
    def test_restart_longer_same_poll(self):
        self.write("A"*300+"\n"+GOOD, 'w'); self.w.poll()
        self.write("B"*300+"\n"+"y"*2000+"\n"+BAD, 'w'); self.w.poll()
        self.assertEqual(self.ev, ['ok','arranque'])
        self.write("z\n"); self.w.poll(); self.write("z\n"); self.w.poll()
        self.assertEqual(self.ev, ['ok','arranque'])
    def test_old_log_ignored(self):
        self.write(BAD,'w'); old=time.time()-3600; os.utime(self.log,(old,old)); self.w.poll(); self.assertEqual(self.ev, [])
    def test_settings_save_keeps_comments(self):
        p=self.d/'u.ini'; p.write_text("; comentario\n[launcher]\nalert_seconds = 5\n[game]\n; donde\ndir = \n[environment]\nUNDS_TRIGGER = 30,95\n",encoding='utf-8')
        s=L.Settings(p); s.save_game_dir(Path('/x/uncharted-windows'))
        t=p.read_text(); self.assertIn('; comentario',t); self.assertIn('dir = '+str(Path('/x/uncharted-windows')),t); self.assertIn('; donde',t)
        s2=L.Settings(p); self.assertEqual(s2.get('game','dir'),str(Path('/x/uncharted-windows'))); self.assertEqual(s2.number('launcher','alert_seconds'),5)
        self.assertEqual(s2.environment(), {'UNDS_TRIGGER':'30,95'})
    def test_settings_new_file(self):
        p=self.d/'n.ini'; s=L.Settings(p); self.assertEqual(s.environment(), L.DEFAULT_ENV)
        s.save_game_dir(Path('/g')); self.assertEqual(L.Settings(p).get('game','dir'),str(Path('/g')))
    def test_find_game(self):
        g=self.d/'uncharted-windows'; (g/'bin').mkdir(parents=True); (g/'Uncharted.exe').write_bytes(b''); (g/'bin'/'bb-probe.exe').write_bytes(b'xxUNDS_EVENTSyyUNDS_FXzz')
        u=g/'UNDS'; u.mkdir()
        self.assertEqual(L.find_game_dir('', '', u), g.resolve())
        self.assertIsNone(L.find_game_dir(str(self.d/'nada'), '', u))
        self.assertEqual(L.probe_features(g/'bin'/'bb-probe.exe'), {'UNDS_EVENTS','UNDS_FX'})
    def test_env_mode(self):
        import io, contextlib
        p=self.d/'e.ini'; p.write_text("[launcher]\nauto = yes\n[environment]\nUNDS_TRIGGER = 30,95\nOTRA = 1\n",encoding='utf-8')
        out=io.StringIO()
        with contextlib.redirect_stdout(out): rc=L.main(['--env','--settings',str(p)])
        self.assertEqual(rc,0); self.assertEqual(out.getvalue().split(), ['UNDS_EVENTS=127.0.0.1:47111','UNDS_TRIGGER=30,95','UNDS_WINDOW=console'])
        p.write_text("[launcher]\nauto = no\n",encoding='utf-8')
        out=io.StringIO()
        with contextlib.redirect_stdout(out): rc=L.main(['--env','--settings',str(p)])
        self.assertEqual((rc,out.getvalue()),(1,''))
    def test_process_alive(self):
        import subprocess, sys
        self.assertTrue(L.process_alive(os.getpid()))
        p=subprocess.Popen([sys.executable,'-c','pass']); p.wait()
        self.assertFalse(L.process_alive(p.pid))
    def test_log_path(self):
        self.assertEqual(L.log_path(Path('/g'), {}), Path('/g/user/last_run.log'))
        self.assertEqual(L.log_path(Path('/g'), {'user_dir':'/s'}), Path('/s/last_run.log'))
if __name__ == "__main__":
    unittest.main()
