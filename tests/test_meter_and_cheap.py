import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import unittest

from helpers import TempHome, ROOT
from local_first import common as C


def load_estimator():
    spec = importlib.util.spec_from_file_location("usage_from_transcripts", os.path.join(ROOT, "meter", "usage_from_transcripts.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def row(ts, rid, **usage):
    return json.dumps({"type": "assistant", "requestId": rid, "uuid": rid,
                       "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)), "message": {"usage": usage}})


class Estimator(TempHome):
    def setUp(self):
        super().setUp()
        self.u = load_estimator()
        self.proj = os.path.join(self.tmp, "proj")
        self.cfg = dict(C.load_config(), projects_dir=self.proj, five_hour_budget_tokens=1000, weekly_budget_tokens=10000,
                        budgets_calibrated=True)
        self.now = time.time()

    def put(self, *rows):
        self.write(os.path.join(self.proj, "p", "a.jsonl"), "\n".join(rows) + "\n")

    def test_windows_dedupe_and_estimate_flag(self):
        self.put(row(self.now - 3600, "r1", input_tokens=100, output_tokens=200),
                 row(self.now - 3600, "r1", input_tokens=100, output_tokens=200),      # same request twice: counted once
                 row(self.now - 86400, "r2", input_tokens=1000, output_tokens=1000),   # inside the week, outside 5 h
                 row(self.now - 9 * 86400, "r3", input_tokens=5000))                    # too old
        d = self.u.compute(self.cfg, self.now)
        self.assertEqual((d["five_hour"], d["weekly"], d["zone"]), (30, 23, "GREEN"))
        self.assertTrue(d["estimate"])

    def test_zone_uses_the_configured_bands(self):
        self.put(row(self.now - 60, "r1", output_tokens=850))
        self.assertEqual(self.u.compute(self.cfg, self.now)["zone"], "AMBER")
        self.put(row(self.now - 60, "r1", output_tokens=1200))
        self.assertEqual(self.u.compute(self.cfg, self.now)["zone"], "RED")
        cfg = dict(self.cfg, bands=dict(self.cfg["bands"], amber_five_hour=95))
        self.put(row(self.now - 60, "r1", output_tokens=850))
        self.assertEqual(self.u.compute(cfg, self.now)["zone"], "GREEN")

    def test_uncalibrated_budgets_never_show_a_band(self):
        self.put(row(self.now - 60, "r1", output_tokens=1200))                      # would be RED
        cfg = dict(self.cfg, budgets_calibrated=False)
        self.assertEqual(self.u.compute(cfg, self.now)["zone"], "UNCALIBRATED")

    def test_cache_reads_optional_and_empty_dir(self):
        self.put(row(self.now - 60, "r1", output_tokens=10, cache_read_input_tokens=500))
        self.assertEqual(self.u.compute(self.cfg, self.now)["five_hour"], 1)
        self.assertEqual(self.u.compute(dict(self.cfg, count_cache_reads=True), self.now)["five_hour"], 51)
        shutil.rmtree(self.proj)
        self.assertEqual(self.u.compute(self.cfg, self.now)["five_hour"], 0)

    def test_cli_writes_usage_file(self):
        self.put(row(self.now - 60, "r1", output_tokens=100))
        self.write(os.path.join(self.lf, "config.json"), json.dumps({"projects_dir": self.proj}))
        subprocess.run([sys.executable, os.path.join(ROOT, "meter", "usage_from_transcripts.py")], check=True)
        self.assertIn("five_hour", json.load(open(os.path.join(self.lf, "usage.json"))))

    def test_makes_no_network_calls_and_no_private_api(self):
        import re
        src = open(os.path.join(ROOT, "meter", "usage_from_transcripts.py")).read()
        for mod in ("urllib", "socket", "requests", "http", "ssl", "subprocess"):
            self.assertIsNone(re.search(rf"^\s*(import|from)\s+{mod}\b", src, re.M), mod)
        self.assertNotIn("claude.ai", src)


class SwiftApp(unittest.TestCase):
    def test_meter_reads_files_only(self):
        src = open(os.path.join(ROOT, "meter", "UsageMeter.swift")).read()
        for word in ("URLSession", "NSURLConnection", "WKWebView", "HTTPCookieStorage"):
            self.assertNotIn(word, src)
        for needle in ("suggestions.json", "Never suggest this again", "Open cheap mode (Terminal)", "Accept", "Not now"):
            self.assertIn(needle, src)


class StatusLine(TempHome):
    def run_line(self, stdin, **env):
        e = dict(os.environ, CHEAP_CACHE_DIR=os.path.join(self.tmp, "cache"), CHEAP_INBOX=os.path.join(self.tmp, "INBOX.md"),
                 OPENROUTER_API_KEY="", ANTHROPIC_BASE_URL="")
        e.update(env)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "cheap", "statusline.py")], input=json.dumps(stdin), capture_output=True,
                           text=True, env=e, timeout=20)
        return r.stdout

    def test_shows_model_folder_credit_and_inbox(self):
        os.makedirs(os.path.join(self.tmp, "cache"))
        json.dump({"left": 7.5, "at": time.time()}, open(os.path.join(self.tmp, "cache", "openrouter_credit.json"), "w"))
        self.write(os.path.join(self.tmp, "INBOX.md"), "## one\n\ntext\n\n## two\n\ntext\n")
        out = self.run_line({"model": {"id": "z-ai/glm-5.3"}, "workspace": {"current_dir": self.home + "/proj"}})
        plain = out.replace("\033[0m", "")
        self.assertIn("CHEAP MODE", out)
        self.assertIn("GLM 5.3", out)
        self.assertIn("~/proj", out)
        self.assertIn("7.50 left", out)
        self.assertIn("inbox 2", out)

    def test_label_is_configurable_and_survives_garbage_input(self):
        out = subprocess.run([sys.executable, os.path.join(ROOT, "cheap", "statusline.py")], input="not json", capture_output=True, text=True,
                             env=dict(os.environ, CHEAP_LABEL="BUDGET MODE", CHEAP_CACHE_DIR=os.path.join(self.tmp, "c2"),
                                      OPENROUTER_API_KEY="", ANTHROPIC_BASE_URL="", CHEAP_INBOX=os.path.join(self.tmp, "none.md")),
                             timeout=20).stdout
        self.assertIn("BUDGET MODE", out)
        self.assertIn("credit", out)


@unittest.skipUnless(shutil.which("zsh"), "needs zsh")
class Launcher(TempHome):
    def launch(self, *args, **env):
        fake = os.path.join(self.tmp, "fakeclaude")
        self.write(fake, '#!/bin/sh\nfor a in "$@"; do echo "ARG<$a>"; done\necho "BASE=$ANTHROPIC_BASE_URL"\necho "MODEL=$ANTHROPIC_MODEL"\n'
                         'echo "LEAK=${LEAKED_SECRET:-none}"\n')
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR)
        e = dict(os.environ, CLAUDE_BIN=fake, OPENROUTER_API_KEY="test-value-not-a-key", LEAKED_SECRET="should-not-pass")
        e.update(env)
        return subprocess.run(["zsh", os.path.join(ROOT, "bin", "claude-cheap"), *args], capture_output=True, text=True, env=e, timeout=30).stdout

    def test_flags_env_isolation_and_settings(self):
        out = self.launch("haiku", "-p", "hello")
        self.assertIn("ARG<--settings>", out)
        self.assertIn("ARG<--append-system-prompt>", out)
        self.assertIn("ARG<--name>", out)
        self.assertIn("ARG<cheap mode>", out)
        self.assertIn("ARG<-p>", out)
        self.assertIn("BASE=https://openrouter.ai/api", out)
        self.assertIn("LEAK=none", out)                                   # env -i: the caller's environment does not leak through
        settings = json.load(open(os.path.join(self.lf, "cheap", "settings.json")))
        self.assertIn("statusline.py", settings["statusLine"]["command"])
        self.assertIn(os.path.join(self.lf, "INBOX.md"), out)             # default guardrails name the inbox

    def test_guardrails_inbox_and_model_are_configurable(self):
        gfile = self.write(os.path.join(self.tmp, "g.txt"), "Only touch the docs folder.")
        out = self.launch(CHEAP_GUARDRAILS_FILE=gfile, CHEAP_MAIN_MODEL="acme/model-x", CHEAP_STATUSLINE="0")
        self.assertIn("ARG<Only touch the docs folder.>", out)
        self.assertIn("MODEL=acme/model-x", out)
        self.assertNotIn("ARG<--settings>", out)
        out = self.launch(CHEAP_GUARDRAILS="Be brief.", CHEAP_INBOX=os.path.join(self.tmp, "mine.md"))
        self.assertIn("ARG<Be brief.>", out)

    def test_cheap_env_file(self):
        self.write(os.path.join(self.lf, "cheap.env"), 'CHEAP_MAIN_MODEL=from/file\nCHEAP_GUARDRAILS="From the file."\n')
        out = self.launch()
        self.assertIn("MODEL=from/file", out)
        self.assertIn("ARG<From the file.>", out)
        self.assertIn("MODEL=real/env", self.launch(CHEAP_MAIN_MODEL="real/env"))      # real environment wins over the file

    def test_missing_key_stops_with_a_clear_message(self):
        r = subprocess.run(["zsh", os.path.join(ROOT, "bin", "claude-cheap")], capture_output=True, text=True, timeout=30,
                           env=dict(os.environ, OPENROUTER_API_KEY="", CHEAP_KEYCHAIN_SERVICE="local-first-test-service-that-does-not-exist"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("No OpenRouter key", r.stdout)


if __name__ == "__main__":
    unittest.main()
