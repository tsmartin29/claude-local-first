import hashlib
import json
import os
import re

from helpers import TempHome, ROOT
from local_first import common as C, scriptify as SC


def fake_secrets():
    """Built at runtime so this file never contains anything that looks like a real key."""
    return {
        "api": "s" + "k-" + "Ab12Cd34Ef56Gh78Ij90Kl12",
        "github": "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4",
        "bearer": "Bearer abcdefghijklmnop12345",
        "env": "MY_API_TOKEN=hunter2hunter2",
        "flag": "--password hunter2",
        "url": "https://bob:pa55w0rd@example.com/x",
        "payment": "r" + "k_live_" + "A1b2C3d4E5f6G7",
        "account": "acc" + "t_1A2b3C4d5E6f7G",
        "aws": "AKIA" + "ABCDEFGHIJKLMNOP",
        "hex": "a" * 20 + "1b2c3d4e5f" * 3,
    }


class Redaction(TempHome):
    def test_every_secret_shape_is_removed(self):
        for kind, secret in fake_secrets().items():
            out = C.redact(f"curl -X POST {secret} done")
            self.assertIn("<redacted>", out, kind)
            for piece in re.split(r"[=: @/ ]", secret):
                if len(piece) > 12:
                    self.assertNotIn(piece, out, kind)

    def test_normal_text_survives(self):
        text = "python3 /home/me/projects/report.py --days 7 && ls -t out/*.md | head -2"
        self.assertEqual(C.redact(text), text)


def transcript(root, project, session, rows):
    path = os.path.join(root, project, session + ".jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return path


def bash(cmd, day="2026-10-10"):
    return {"type": "assistant", "timestamp": day + "T10:00:00Z",
            "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]}}


class Analyse(TempHome):
    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, "projects")

    def scan(self, **kw):
        return SC.scan(self.root, **kw)

    def test_finds_commands_repeated_across_sessions_and_days(self):
        cmd = "python3 /work/downloads_report.py --days 7"
        transcript(self.root, "p1", "s1", [bash(cmd, "2026-10-10"), bash("ls -la", "2026-10-10")])
        transcript(self.root, "p2", "s2", [bash(cmd, "2026-10-11")])
        transcript(self.root, "p1", "s3", [bash(cmd.replace("7", "14"), "2026-10-12")])     # numbers vary: still the same command
        cands, _ = self.scan()
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual((c["count"], c["sessions"], c["days"], c["name"]), (3, 3, 3, "downloads-report"))
        self.assertTrue(SC.localjob_line(c).startswith("localjob add downloads-report --every"))

    def test_one_session_one_day_is_not_a_pattern(self):
        transcript(self.root, "p1", "s1", [bash("python3 /work/a.py")] * 5)
        self.assertEqual(self.scan()[0], [])

    def test_below_min_count_ignored_and_trivial_ignored(self):
        transcript(self.root, "p1", "s1", [bash("python3 /w/a.py", "2026-10-10"), bash("cd /tmp", "2026-10-10")] * 1)
        transcript(self.root, "p1", "s2", [bash("python3 /w/a.py", "2026-10-11"), bash("cd /tmp", "2026-10-11")])
        transcript(self.root, "p1", "s3", [bash("cd /tmp", "2026-10-12")])
        self.assertEqual(self.scan()[0], [])                           # a.py only twice; `cd` is trivial

    def test_git_and_heredocs_skipped(self):
        for i in range(3):
            transcript(self.root, "p", f"s{i}", [bash("git status --short --branch", f"2026-10-1{i}"),
                                                 bash("cat > f.txt <<'EOF'\nhello world\nEOF", f"2026-10-1{i}")])
        self.assertEqual(self.scan()[0], [])
        self.assertEqual(len(self.scan(include_git=True)[0]), 1)

    def test_risky_commands_are_flagged(self):
        for i in range(3):
            transcript(self.root, "p", f"s{i}", [bash("node release.mjs deploy --yes", f"2026-10-1{i}")])
        c = self.scan()[0][0]
        self.assertTrue(c["risky"])

    def test_secrets_never_appear_in_output(self):
        s = fake_secrets()
        cmd = f"curl -s -H 'Authorization: {s['bearer']}' https://api.example.com/v1/usage?key={s['api']} && echo {s['github']}"
        for i in range(3):
            transcript(self.root, "p", f"s{i}", [bash(cmd, f"2026-10-1{i}")])
        cands, _ = self.scan()
        self.assertEqual(len(cands), 1)
        blob = json.dumps(cands) + SC.localjob_line(cands[0])
        for secret in (s["api"], s["github"], "abcdefghijklmnop12345"):
            self.assertNotIn(secret, blob)
        self.assertIn("<redacted>", blob)

    def test_sequences_of_shell_steps(self):
        steps = [bash("python3 /w/a.py"), bash("curl -s http://localhost:1/x"), bash("osascript -e 'beep'")]
        for i in range(3):
            transcript(self.root, "p", f"s{i}", steps)
        _, seqs = self.scan()
        self.assertEqual(seqs[0]["steps"], ["python3", "curl", "osascript"])
        self.assertEqual(seqs[0]["sessions"], 3)

    def test_transcripts_are_not_modified(self):
        path = transcript(self.root, "p", "s1", [bash("python3 /w/a.py")] * 3)
        before = (hashlib.sha1(open(path, "rb").read()).hexdigest(), os.path.getmtime(path))
        self.scan()
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            SC.main(["--root", self.root])
        self.assertEqual((hashlib.sha1(open(path, "rb").read()).hexdigest(), os.path.getmtime(path)), before)

    def test_module_has_no_networking(self):
        src = open(os.path.join(ROOT, "local_first", "scriptify.py")).read()
        for mod in ("urllib", "socket", "http", "requests", "smtplib", "ftplib", "subprocess"):
            self.assertIsNone(re.search(rf"^\s*(import|from)\s+{mod}\b", src, re.M), mod)

    def test_cli_output(self):
        import io
        from contextlib import redirect_stdout
        for i in range(3):
            transcript(self.root, "p", f"s{i}", [bash("python3 /w/report.py --out /w/o.md", f"2026-10-1{i}")])
        out = io.StringIO()
        with redirect_stdout(out):
            SC.main(["--root", self.root])
        self.assertIn("seen 3x in 3 sessions on 3 days", out.getvalue())
        self.assertIn("localjob add report", out.getvalue())


if __name__ == "__main__":
    import unittest
    unittest.main()
