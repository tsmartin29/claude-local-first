"""Run: python3 -m unittest discover -s tests -v"""
import importlib, json, os, sys, tempfile, time, unittest, shutil
ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "relay")

class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.environ.update(RELAY_HOME=self.d + "/home", RELAY_NO_NOTIFY="1")
        sys.path.insert(0, ROOT); sys.modules.pop("relay", None)
        self.r = importlib.import_module("relay")
        self.r.HOME = self.d                       # so the "Trash" used by housekeeping is inside the temp dir
        self.cfg = dict(self.r.DEFAULTS, workdir=self.d, inbox=self.d + "/inbox/INBOX.md", queue_dir=self.d + "/inbox/queue",
                        usage_file=self.d + "/usage.json", transcripts_dir=self.d + "/proj", check_ram=False, notify=False, auto_run=True,
                        cheap_command=["sh", "-c", "echo cheap-ran; echo \"$0\" | head -c 40", "{prompt}"])
        os.makedirs(self.d + "/proj/p")
        self.now = time.time()
    def tearDown(self): shutil.rmtree(self.d)
    def usage(self, **k): json.dump(k, open(self.cfg["usage_file"], "w"))
    def wait_done(self):
        lock = self.d + "/home/cheap.lock"
        for _ in range(100):
            if not os.path.exists(lock): return
            time.sleep(0.05)
        self.fail("cheap run did not finish")

    def test_available_when_usage_ok(self):
        self.usage(five_hour=40, weekly=30)
        self.assertEqual(self.r.run(self.cfg, self.now)["status"], "AVAILABLE")
        self.assertFalse(os.path.exists(self.d + "/home/cheap.lock"))

    def test_empty_starts_one_cheap_run_with_cooldown(self):
        self.usage(five_hour=100, weekly=40)
        st = self.r.run(self.cfg, self.now); self.assertEqual(st["status"], "EMPTY")
        self.wait_done()
        self.assertIn("cheap-ran", open(self.d + "/home/cheap_last.log").read())
        first = st["last_cheap"]
        st = self.r.run(self.cfg, self.now + 600)       # 10 minutes later: still inside the cooldown
        self.assertEqual(st["last_cheap"], first)

    def test_without_auto_run_empty_usage_starts_nothing(self):
        self.usage(five_hour=100, weekly=40)
        st = self.r.run(dict(self.cfg, auto_run=False), self.now)
        self.assertEqual(st["status"], "EMPTY"); self.assertEqual(st["last_cheap"], 0)
        self.assertFalse(os.path.exists(self.d + "/home/cheap.lock"))

    def test_extra_usage_left_means_not_empty(self):
        self.usage(five_hour=100, weekly=40, extra_spent=5, extra_limit=50)
        self.assertEqual(self.r.run(self.cfg, self.now)["status"], "AVAILABLE")

    def test_normal_reply_beats_old_limit_error(self):
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now - 60))
        err = json.dumps({"type": "assistant", "isApiErrorMessage": True, "message": "You hit your limit"}).replace('": ', '":')
        ok = json.dumps({"type": "assistant", "timestamp": ts, "message": {"content": "hi"}}).replace('": ', '":')
        self.usage(five_hour=10)
        open(self.d + "/proj/p/a.jsonl", "w").write(err + "\n")
        self.assertEqual(self.r.run(self.cfg, self.now)["status"], "EMPTY"); self.wait_done()
        open(self.d + "/proj/p/a.jsonl", "a").write(ok + "\n")
        self.assertEqual(self.r.run(self.cfg, self.now + 1)["status"], "AVAILABLE")
        self.assertIn("Claude usage is back", open(self.cfg["inbox"]).read())

    def test_ram_gate_holds_cheap_run(self):
        self.usage(five_hour=100)
        self.r.ram_ok = lambda cfg: (False, "swap at 9.0 GB")
        st = self.r.run(self.cfg, self.now)
        self.assertEqual(st["status"], "EMPTY"); self.assertEqual(st["last_cheap"], 0); self.assertIn("swap", st["ram_hold"])

    def test_digest_has_last_messages_and_no_api_calls(self):
        rows = [json.dumps({"type": "user", "cwd": "/work/app", "message": {"content": "fix the login bug"}}),
                json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Done, tests pass"}]}})]
        open(self.d + "/proj/p/s.jsonl", "w").write("\n".join(rows) + "\n")
        self.r.write_digest(self.cfg, self.now, self.d + "/digest.md")
        t = open(self.d + "/digest.md").read()
        self.assertIn("fix the login bug", t); self.assertIn("Done, tests pass", t); self.assertIn("/work/app", t)

    def test_housekeeping_moves_old_done_files_to_trash(self):
        os.makedirs(self.cfg["queue_dir"] + "/done")
        old = self.cfg["queue_dir"] + "/done/old.md"; open(old, "w").write("x"); os.utime(old, (self.now - 9 * 86400,) * 2)
        new = self.cfg["queue_dir"] + "/done/new.md"; open(new, "w").write("x")
        self.assertEqual(self.r.housekeeping(self.cfg, self.now, []), 1)
        self.assertFalse(os.path.exists(old)); self.assertTrue(os.path.exists(new))
        self.assertTrue(any(f.startswith("old.md") for f in os.listdir(self.d + "/.Trash")))

if __name__ == "__main__": unittest.main()
