import io
import json
import os
import threading
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer

from helpers import TempHome
from local_first import common as C, decisions as D, ladder as LD, miniyaml


def task(**kw):
    raw = {"name": "t", "prompt": "Summarise.", "tier0": {"command": ["true"]}}
    raw.update(kw)
    return LD.normalize_task(raw, "/tmp")


def calls_recorder(fail=()):
    calls = []

    def make(tier):
        def run(t, cfg, ctx):
            calls.append(tier)
            if tier in fail:
                raise RuntimeError(f"tier {tier} broke")
            return f"out{tier}"
        return run
    return calls, {0: make(0), 1: make(1), 2: make(2)}


ALL = lambda tier, t, cfg: (True, "")


class MiniYaml(TempHome):
    def test_task_file(self):
        y = miniyaml.loads("""
# comment
name: digest   # trailing comment
tier0:
  command: ["python3", "a b.py"]
  timeout: 30
inputs:
  - a.md
  - 'b c.md'
never_cheap: false
prompt: |
  Line one
  Line two
max_tier: 2
""")
        self.assertEqual(y["name"], "digest")
        self.assertEqual(y["tier0"], {"command": ["python3", "a b.py"], "timeout": 30})
        self.assertEqual(y["inputs"], ["a.md", "b c.md"])
        self.assertIs(y["never_cheap"], False)
        self.assertEqual(y["prompt"], "Line one\nLine two\n")

    def test_json_and_errors(self):
        self.assertEqual(miniyaml.loads('{"a": 1}'), {"a": 1})
        with self.assertRaises(miniyaml.YamlError):
            miniyaml.loads("a: {b: 1}")
        with self.assertRaises(miniyaml.YamlError):
            miniyaml.loads("a:\n  - b: 1")

    def test_load_task_yaml_and_json(self):
        self.write(os.path.join(self.tmp, "x.yaml"), "tier0: echo hello\ninputs: [n.md]\n")
        t = LD.load_task(os.path.join(self.tmp, "x.yaml"))
        self.assertEqual((t["name"], t["tier0"]["argv"], t["home_tier"]), ("x", ["echo", "hello"], 0))
        self.assertEqual(t["inputs"], [os.path.join(self.tmp, "n.md")])
        self.write(os.path.join(self.tmp, "y.json"), json.dumps({"prompt": "p"}))
        self.assertEqual(LD.load_task(os.path.join(self.tmp, "y.json"))["home_tier"], 3)

    def test_bad_tasks(self):
        with self.assertRaises(LD.TaskError):
            LD.normalize_task({}, "/tmp")
        with self.assertRaises(LD.TaskError):
            LD.normalize_task({"prompt": "p", "never_cheap": True, "tier": 1}, "/tmp")


class FallbackOrder(TempHome):
    def setUp(self):
        super().setUp()
        self.cfg = C.load_config()

    def test_tier0_wins_and_nothing_else_runs(self):
        calls, runners = calls_recorder()
        r = LD.run_task(task(), self.cfg, avail=ALL, runners=runners)
        self.assertEqual((r["tier_ran"], r["status"], calls), (0, "ok", [0]))

    def test_falls_up_the_ladder_in_order(self):
        calls, runners = calls_recorder(fail=(0, 1))
        r = LD.run_task(task(), self.cfg, avail=ALL, runners=runners)
        self.assertEqual(calls, [0, 1, 2])
        self.assertEqual((r["tier_ran"], r["output"]), (2, "out2"))
        self.assertEqual([a["status"] for a in r["attempts"]], ["failed", "failed", "ok"])

    def test_unavailable_tiers_are_skipped_not_run(self):
        calls, runners = calls_recorder()
        avail = lambda tier, t, cfg: (tier != 0, "no")
        r = LD.run_task(task(tier=0), self.cfg, avail=avail, runners=runners)
        self.assertEqual(calls, [1])
        self.assertEqual(r["attempts"][0]["status"], "unavailable")

    def test_all_fail_ends_in_handoff_not_a_claude_call(self):
        calls, runners = calls_recorder(fail=(0, 1, 2))
        r = LD.run_task(task(), self.cfg, avail=ALL, runners=runners)
        self.assertEqual(calls, [0, 1, 2])
        self.assertEqual((r["status"], r["tier_ran"]), ("handoff", 3))
        self.assertIn("needs Claude", open(self.cfg["inbox"]).read())
        self.assertIn("Summarise.", open(r["handoff"]).read())

    def test_claude_only_runs_with_allow_claude(self):
        calls, runners = calls_recorder(fail=(0, 1, 2))
        runners[3] = lambda t, cfg, ctx: "claude says hi"
        self.assertEqual(LD.run_task(task(), self.cfg, avail=ALL, runners=runners)["status"], "handoff")
        r = LD.run_task(task(), self.cfg, avail=ALL, runners=runners, allow_claude=True)
        self.assertEqual((r["status"], r["tier_ran"], r["output"]), ("ok", 3, "claude says hi"))

    def test_max_tier_caps_the_climb(self):
        calls, runners = calls_recorder(fail=(0, 1))
        r = LD.run_task(task(max_tier=1), self.cfg, avail=ALL, runners=runners)
        self.assertEqual(calls, [0, 1])
        self.assertEqual(r["status"], "failed")

    def test_script_only_task_fails_without_inventing_a_handoff(self):
        calls, runners = calls_recorder(fail=(0,))
        r = LD.run_task(LD.normalize_task({"name": "s", "tier0": {"command": ["x"]}}, "/tmp"), self.cfg, avail=ALL, runners=runners)
        self.assertEqual((r["status"], r["handoff"]), ("failed", None))


class NeverCheap(TempHome):
    def setUp(self):
        super().setUp()
        self.cfg = C.load_config()

    def test_never_cheap_skips_every_cheaper_tier(self):
        calls, runners = calls_recorder()
        t = task(never_cheap=True)
        self.assertEqual(t["home_tier"], 3)
        r = LD.run_task(t, self.cfg, avail=ALL, runners=runners)
        self.assertEqual(calls, [])                                    # no script, no local model, no cheap API
        self.assertEqual((r["status"], r["tier_ran"]), ("handoff", 3))
        self.assertIn("never_cheap", open(r["handoff"]).read())

    def test_accepted_move_cannot_override_never_cheap(self):
        D.record("t-t", "accepted", 1, task="t", to_tier=0)
        t = task(never_cheap=True)
        self.assertEqual(LD.effective_start(t), 3)
        plan = LD.plan(t, self.cfg, avail=ALL)
        self.assertTrue(all(p["status"] == "blocked" for p in plan[:3]))

    def test_tokens_saved_only_for_runs_that_stayed_local(self):
        self.write(os.path.join(self.tmp, "in.md"), "x" * 4000)
        t = LD.normalize_task({"name": "t", "prompt": "p" * 100, "tier0": {"command": ["true"]}, "inputs": ["in.md"]}, self.tmp)
        calls, runners = calls_recorder()
        r = LD.run_task(t, self.cfg, avail=ALL, runners=runners)
        self.assertEqual(r["est_tokens_not_sent"], 4000 // 4)          # the script's fallback prompt is not counted
        calls, runners = calls_recorder(fail=(0,))
        self.assertEqual(LD.run_task(t, self.cfg, avail=ALL, runners=runners)["est_tokens_not_sent"], (4000 + 100) // 4)   # local model ran
        self.assertTrue(r["estimate"])
        calls, runners = calls_recorder(fail=(0, 1))
        self.assertEqual(LD.run_task(t, self.cfg, avail=ALL, runners=runners)["est_tokens_not_sent"], 0)   # went to an API


class StartTier(TempHome):
    def setUp(self):
        super().setUp()
        self.cfg = C.load_config()

    def test_prompt_task_starts_at_claude_until_you_accept(self):
        t = LD.normalize_task({"name": "sum", "prompt": "p"}, "/tmp")
        calls, runners = calls_recorder()
        r = LD.run_task(t, self.cfg, avail=ALL, runners=runners)
        self.assertEqual((calls, r["status"]), ([], "handoff"))          # nothing cheaper without your OK
        D.record("t-sum", "accepted", 1, task="sum", to_tier=2)
        calls, runners = calls_recorder()
        r = LD.run_task(t, self.cfg, avail=ALL, runners=runners)
        self.assertEqual((calls, r["tier_ran"]), ([2], 2))

    def test_declined_and_never_leave_start_unchanged(self):
        t = LD.normalize_task({"name": "sum", "prompt": "p"}, "/tmp")
        for status in ("declined", "never"):
            D.record("t-sum", status, 1, task="sum")
            self.assertEqual(LD.effective_start(t), 3)

    def test_explicit_tier_is_your_own_choice(self):
        t = LD.normalize_task({"name": "sum", "prompt": "p", "tier": 1}, "/tmp")
        calls, runners = calls_recorder()
        LD.run_task(t, self.cfg, avail=ALL, runners=runners)
        self.assertEqual(calls, [1])


class OllamaStub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"models": [{"name": "tiny:1b"}]}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        req = json.loads(self.rfile.read(n))
        body = json.dumps({"response": f"[{req['model']}] saw {len(req['prompt'])} chars"}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)


class Tier1Ollama(TempHome):
    def test_http_client_against_stub(self):
        srv = HTTPServer(("127.0.0.1", 0), OllamaStub)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_port}"
        try:
            self.assertEqual(LD.ollama_models(url), ["tiny:1b"])
            cfg = dict(C.load_config(), ollama_url=url)
            t = LD.normalize_task({"name": "o", "prompt": "hello", "tier": 1}, "/tmp")
            self.assertEqual(LD.available(1, t, cfg), (True, ""))
            r = LD.run_task(t, cfg)
            self.assertEqual((r["tier_ran"], r["status"]), (1, "ok"))
            self.assertIn("[tiny:1b] saw 5 chars", r["output"])
        finally:
            srv.shutdown()

    def test_ollama_down_means_unavailable(self):
        cfg = dict(C.load_config(), ollama_url="http://127.0.0.1:9")
        t = LD.normalize_task({"name": "o", "prompt": "hello"}, "/tmp")
        ok, why = LD.available(1, t, cfg)
        self.assertFalse(ok)
        self.assertIn("Ollama", why)

    def test_inputs_redacted_before_api_but_not_for_local_model(self):
        fake = "s" + "k-" + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss"
        self.write(os.path.join(self.tmp, "n.md"), f"key {fake}")
        t = LD.normalize_task({"name": "o", "prompt": "p", "inputs": ["n.md"]}, self.tmp)
        cfg = C.load_config()
        self.assertNotIn(fake, LD.build_prompt(t, cfg, for_api=True))
        self.assertIn(fake, LD.build_prompt(t, cfg, for_api=False))


class Cli(TempHome):
    def test_run_script_task_prints_output_and_records_stats(self):
        self.write(os.path.join(self.tmp, "r.yaml"), "name: rep\ntier0: echo report-body\n")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = LD.main(["run", os.path.join(self.tmp, "r.yaml")])
        self.assertEqual(rc, 0)
        self.assertIn("report-body", out.getvalue())
        self.assertIn("tier 0 (script)", err.getvalue())
        rows = LD.read_stats()
        self.assertEqual((rows[0]["task"], rows[0]["tier_ran"]), ("rep", 0))
        self.assertEqual(LD.summarize_stats(rows)["runs"], 1)

    def test_plan_dry_run_runs_nothing(self):
        marker = os.path.join(self.tmp, "ran")
        self.write(os.path.join(self.tmp, "r.yaml"), f"name: rep\ntier0: touch {marker}\n")
        out = io.StringIO()
        with redirect_stdout(out):
            LD.main(["run", os.path.join(self.tmp, "r.yaml"), "--dry-run"])
        self.assertFalse(os.path.exists(marker))
        self.assertIn("starts at tier 0", out.getvalue())


if __name__ == "__main__":
    import unittest
    unittest.main()
