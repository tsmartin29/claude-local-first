import io
import json
import os
import plistlib
import sys
import time
from contextlib import redirect_stdout, redirect_stderr

from helpers import TempHome
from local_first import common as C, localjob as L


def spec_for(name="demo", every="15m", **kw):
    return dict({"name": name, "argv": ["echo", "hi"], "schedule": every, "schedule_parsed": L.parse_schedule(every)}, **kw)


class ScheduleParsing(TempHome):
    def test_intervals(self):
        self.assertEqual(L.parse_schedule("2h"), {"kind": "interval", "seconds": 7200})
        self.assertEqual(L.parse_schedule("15m")["seconds"], 900)
        self.assertEqual(L.parse_schedule("30s")["seconds"], 30)
        self.assertEqual(L.parse_schedule("1d")["seconds"], 86400)

    def test_daily(self):
        self.assertEqual(L.parse_schedule("daily@06:47"), {"kind": "calendar", "hour": 6, "minute": 47})
        self.assertEqual(L.parse_schedule("daily@6:05")["hour"], 6)

    def test_bad_schedules(self):
        for bad in ("", "soon", "0m", "2x", "daily@25:00", "daily@06:70", "1s"):
            with self.assertRaises(ValueError, msg=bad):
                L.parse_schedule(bad)

    def test_human(self):
        self.assertEqual(L.human_schedule(L.parse_schedule("2h")), "every 2h")
        self.assertEqual(L.human_schedule(L.parse_schedule("daily@03:30")), "daily at 03:30")


class UnitGeneration(TempHome):
    def test_plist_interval(self):
        cfg = C.load_config()
        d = plistlib.loads(L.build_plist(spec_for(), cfg, python="/usr/bin/python3", wrapper="/x/bin/localjob"))
        self.assertEqual(d["Label"], "com.local-first.demo")
        self.assertEqual(d["ProgramArguments"], ["/usr/bin/python3", "/x/bin/localjob", "_wrap", "demo"])
        self.assertEqual(d["StartInterval"], 900)
        self.assertNotIn("StartCalendarInterval", d)
        self.assertFalse(d["RunAtLoad"])
        self.assertTrue(d["StandardOutPath"].endswith("demo.launchd.log"))
        self.assertEqual(d["WorkingDirectory"], self.lf)       # never a protected folder
        self.assertIn(self.home + "/.local/bin", d["EnvironmentVariables"]["PATH"])

    def test_plist_calendar_and_run_at_load(self):
        cfg = C.load_config()
        d = plistlib.loads(L.build_plist(spec_for(every="daily@06:47", run_at_load=True), cfg, python="p", wrapper="w"))
        self.assertEqual(d["StartCalendarInterval"], {"Hour": 6, "Minute": 47})
        self.assertNotIn("StartInterval", d)
        self.assertTrue(d["RunAtLoad"])

    def test_systemd_and_cron(self):
        cfg = C.load_config()
        service, timer = L.build_systemd(spec_for(every="daily@06:47"), cfg, python="p", wrapper="w")
        self.assertIn("ExecStart=p w _wrap demo", service)
        self.assertIn("OnCalendar=*-*-* 06:47:00", timer)
        _, timer = L.build_systemd(spec_for(every="2h"), cfg, python="p", wrapper="w")
        self.assertIn("OnUnitActiveSec=7200s", timer)
        self.assertTrue(L.cron_line(spec_for(every="2h"), cfg, "p", "w").startswith("0 */2 * * * "))
        self.assertTrue(L.cron_line(spec_for(every="15m"), cfg, "p", "w").startswith("*/15 * * * * "))
        self.assertTrue(L.cron_line(spec_for(every="daily@06:47"), cfg, "p", "w").startswith("47 6 * * * "))
        with self.assertRaises(ValueError):
            L.cron_line(spec_for(every="90s"), cfg, "p", "w")


class AddListRemove(TempHome):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = L.main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_dry_run_writes_nothing(self):
        rc, out, _ = self.run_cli("add", "demo", "--every", "2h", "--dry-run", "--", "echo", "hi")
        self.assertEqual(rc, 0)
        self.assertIn("StartInterval", out)
        self.assertFalse(os.path.exists(os.path.join(self.lf, "jobs")))
        self.assertFalse(os.path.exists(os.path.join(self.home, "Library")))

    def test_add_list_remove(self):
        rc, out, _ = self.run_cli("add", "demo", "--every", "daily@06:47", "--heavy", "--min-avail-gb", "3", "--", "echo", "hi")
        self.assertEqual(rc, 0)
        plist = os.path.join(self.home, "Library", "LaunchAgents", "com.local-first.demo.plist")
        self.assertEqual(plistlib.load(open(plist, "rb"))["StartCalendarInterval"]["Hour"], 6)
        spec = json.load(open(os.path.join(self.lf, "jobs", "demo.json")))
        self.assertEqual((spec["heavy"], spec["min_avail_gb"], spec["argv"]), ("full", 3.0, ["echo", "hi"]))
        _, out, _ = self.run_cli("list")
        self.assertIn("demo", out)
        self.assertIn("daily at 06:47", out)
        _, out, _ = self.run_cli("remove", "demo")
        self.assertFalse(os.path.exists(plist))                         # plist moved, not deleted
        self.assertTrue(any(f.startswith("com.local-first.demo.plist") for f in os.listdir(os.path.join(self.home, ".Trash"))))
        self.assertIn("No jobs yet", self.run_cli("list")[1])

    def test_bad_input(self):
        with self.assertRaises(SystemExit):
            self.run_cli("add", "Bad Name", "--every", "2h", "--", "echo")
        with self.assertRaises(SystemExit):
            self.run_cli("add", "ok", "--every", "never", "--", "echo")
        with self.assertRaises(SystemExit):
            self.run_cli("add", "ok", "--every", "2h")                   # no command

    def test_readd_replaces_and_trashes_old_plist(self):
        for _ in range(2):
            self.run_cli("add", "demo", "--every", "2h", "--", "echo", "hi")
        self.assertEqual(len([f for f in os.listdir(os.path.join(self.home, ".Trash")) if "demo" in f]), 1)

    def test_protected_folder_detection(self):
        self.assertTrue(C.under_protected(os.path.join(self.home, "Desktop", "x", "y.sh")))
        self.assertTrue(C.under_protected("~/Documents"))
        self.assertFalse(C.under_protected(os.path.join(self.home, ".local-first", "data")))
        _, _, err = self.run_cli("add", "demo", "--every", "2h", "--dry-run", "--", "sh", os.path.join(self.home, "Desktop", "a.sh"))
        if C.IS_MAC:
            self.assertIn("Full Disk Access", err)


class GateLogic(TempHome):
    GB = 1024 ** 3

    def test_blocks_on_low_memory_or_disk(self):
        self.assertEqual(C.gate_check(2, 5, res={"avail": 8 * self.GB, "disk": 50 * self.GB, "swap": 0})[0], True)
        ok, why = C.gate_check(2, 5, res={"avail": 1 * self.GB, "disk": 50 * self.GB, "swap": 0})
        self.assertFalse(ok)
        self.assertIn("memory", why)
        ok, why = C.gate_check(2, 5, res={"avail": 8 * self.GB, "disk": 3 * self.GB, "swap": 0})
        self.assertFalse(ok)
        self.assertIn("disk", why)
        ok, why = C.gate_check(0, 0, 4, res={"avail": 8 * self.GB, "disk": 9 * self.GB, "swap": 6 * self.GB})
        self.assertFalse(ok)
        self.assertIn("swap", why)

    def test_unreadable_values_fail_open(self):
        self.assertEqual(C.gate_check(2, 5, 1, res={"avail": None, "disk": None, "swap": None}), (True, ""))

    def test_vm_stat_parser(self):
        text = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free:   100.\nPages active: 999.\n"
                "Pages inactive:  50.\nPages speculative:  10.\nPages purgeable:  5.\n")
        self.assertEqual(C.parse_vm_stat(text), (100 + 50 + 10 + 5) * 16384)


class RunJob(TempHome):
    def add(self, name, code, every="2h", **kw):
        spec = spec_for(name, every, argv=[sys.executable, "-c", code], **kw)
        C.write_json(L.paths(name)["spec"], spec)
        return spec

    def notes(self):
        sent = []
        return sent, (lambda title, msg: sent.append((title, msg)))

    def test_success_logs_and_appends_inbox(self):
        self.add("ok", "print('all good: 42 rows')")
        sent, nf = self.notes()
        r = L.run_job("ok", notify_fn=nf, resources={"avail": None, "disk": None, "swap": None})
        self.assertEqual((r["status"], r["rc"]), ("ok", 0))
        log = open(L.paths("ok")["log"]).read()
        self.assertIn("all good: 42 rows", log)
        self.assertIn("end: ok", log)
        inbox = open(C.load_config()["inbox"]).read()
        self.assertIn("ok · ok", inbox)
        self.assertIn("all good: 42 rows", inbox)
        self.assertEqual(sent, [])
        self.assertEqual(json.load(open(L.paths("ok")["state"]))["last_status"], "ok")

    def test_failure_notifies_once_per_hour(self):
        self.add("bad", "import sys; print('boom'); sys.exit(3)")
        sent, nf = self.notes()
        res = {"avail": None, "disk": None, "swap": None}
        r = L.run_job("bad", notify_fn=nf, resources=res)
        self.assertEqual((r["status"], r["rc"]), ("failed", 3))
        L.run_job("bad", notify_fn=nf, resources=res)
        self.assertEqual(len(sent), 1)                                    # not one per run
        self.assertIn("FAILED", open(C.load_config()["inbox"]).read())
        st = json.load(open(L.paths("bad")["state"]))
        st["last_fail_notified"] -= 4000
        C.write_json(L.paths("bad")["state"], st)
        L.run_job("bad", notify_fn=nf, resources=res)
        self.assertEqual(len(sent), 2)

    def test_gate_skips_instead_of_running(self):
        marker = os.path.join(self.tmp, "ran")
        self.add("gated", f"open({marker!r}, 'w').write('x')", min_avail_gb=4)
        sent, nf = self.notes()
        low = {"avail": 1024 ** 3, "disk": 100 * 1024 ** 3, "swap": 0}
        r = L.run_job("gated", notify_fn=nf, resources=low)
        self.assertEqual(r["status"], "skipped")
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(sent, [])                                        # a skip is not a failure
        self.assertIn("skipped:", open(L.paths("gated")["log"]).read())
        r = L.run_job("gated", force=True, notify_fn=nf, resources=low)   # --force ignores the gate
        self.assertEqual(r["status"], "ok")
        self.assertTrue(os.path.exists(marker))

    def test_inbox_modes(self):
        self.assertEqual(L.inbox_mode(spec_for(every="15s", inbox="auto")), "fail")
        self.assertEqual(L.inbox_mode(spec_for(every="2h", inbox="auto")), "always")
        self.assertEqual(L.inbox_mode(spec_for(every="daily@03:00", inbox="auto")), "always")
        self.add("quiet", "print('fine')", every="15m")
        L.run_job("quiet", notify_fn=lambda *a: None)
        self.assertFalse(os.path.exists(C.load_config()["inbox"]))        # fast job: inbox only on failure

    def test_tcc_error_prints_exact_fix(self):
        self.add("tcc", "import sys; sys.stderr.write('ls: /x/Desktop: Operation not permitted\\n'); sys.exit(1)")
        sent, nf = self.notes()
        r = L.run_job("tcc", notify_fn=nf)
        self.assertEqual(r["status"], "failed")
        log = open(L.paths("tcc")["log"]).read()
        self.assertIn("Full Disk Access", log)
        self.assertIn("OUTSIDE", log)
        self.assertIn("Full Disk Access", open(C.load_config()["inbox"]).read())
        self.assertIn("privacy", sent[0][1])

    def test_missing_command_is_a_failure(self):
        C.write_json(L.paths("nope")["spec"], spec_for("nope", argv=["/definitely/not/here"]))
        self.assertEqual(L.run_job("nope", notify_fn=lambda *a: None)["status"], "failed")

    def test_timeout_kills_command(self):
        self.add("slow", "import time; time.sleep(30)", timeout=1)
        t = time.time()
        r = L.run_job("slow", notify_fn=lambda *a: None)
        self.assertEqual(r["status"], "failed")
        self.assertLess(time.time() - t, 15)
        self.assertIn("timed out", r["reason"])

    def test_busy_lock(self):
        self.add("busy", "print(1)")
        open(L.paths("busy")["lock"], "w").write(str(os.getpid()))      # a live pid
        self.assertEqual(L.run_job("busy")["status"], "busy")

    def test_log_rotation_sends_oldest_to_trash(self):
        log = L.paths("rot")["log"]
        os.makedirs(os.path.dirname(log))
        for suffix, text in (("", "x" * 100), (".1", "one"), (".2", "two")):
            open(log + suffix, "w").write(text)
        self.assertTrue(L.rotate_log(log, 50, 2))
        self.assertEqual(open(log + ".1").read(), "x" * 100)
        self.assertEqual(open(log + ".2").read(), "one")
        self.assertFalse(os.path.exists(log))
        self.assertTrue(any(f.startswith("rot.log.2") for f in os.listdir(os.path.join(self.home, ".Trash"))))
        self.assertFalse(L.rotate_log(log, 50, 2))

    def test_result_paragraph_redacts_secrets(self):
        fake = "s" + "k-" + "a1b2c3d4e5f6g7h8i9j0k1"
        para = L.one_paragraph("j", "ok", 1.2, f"token is {fake}\n", 0)
        self.assertNotIn(fake, para)
        self.assertIn("<redacted>", para)


if __name__ == "__main__":
    import unittest
    unittest.main()
