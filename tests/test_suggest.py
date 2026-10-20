import io
import json
import os
from contextlib import redirect_stdout

from helpers import TempHome
from local_first import bands as B, cli, common as C, decisions as D, ladder as LD, suggest as S

BANDS = C.DEFAULTS["bands"]
ALL = lambda tier, t, cfg: (True, "")
NONE = lambda tier, t, cfg: (False, "no")


def T(name, **kw):
    raw = {"name": name, "prompt": "p"}
    raw.update(kw)
    return LD.normalize_task(raw, "/tmp")


class Bands(TempHome):
    def test_defaults_match_router(self):
        self.assertEqual(BANDS, {"amber_five_hour": 80, "amber_weekly": 85, "amber_model_weekly": 85, "amber_extra": 90, "red": 100})

    def test_band_thresholds(self):
        cb = lambda **u: B.compute_band(u, BANDS)[0]
        self.assertEqual(cb(), "GREEN")
        self.assertEqual(cb(five_hour=79, weekly=84), "GREEN")
        self.assertEqual(cb(five_hour=80), "AMBER")
        self.assertEqual(cb(weekly=85), "AMBER")
        self.assertEqual(cb(weekly_model=85), "AMBER")
        self.assertEqual(cb(extra_spent=45, extra_limit=50), "AMBER")
        self.assertEqual(cb(five_hour=100), "RED")
        self.assertEqual(cb(weekly=100, five_hour=10), "RED")
        self.assertEqual(cb(five_hour=90, weekly=100), "RED")
        self.assertEqual(B.compute_band(None, BANDS), ("GREEN", []))

    def test_custom_thresholds(self):
        self.assertEqual(B.compute_band({"five_hour": 60}, dict(BANDS, amber_five_hour=50))[0], "AMBER")

    def test_reasons_explain_the_band(self):
        band, reasons = B.compute_band({"five_hour": 83, "weekly": 40}, BANDS)
        self.assertEqual(band, "AMBER")
        self.assertIn("5-hour 83%", reasons[0])

    def test_transitions_count_only_upward_crossings(self):
        st, ev = B.advance(None, "GREEN", 1)
        self.assertEqual((st["crossing"], ev), (0, None))
        st, ev = B.advance(st, "AMBER", 2)
        self.assertEqual((st["crossing"], ev), (1, "up"))
        st, ev = B.advance(st, "AMBER", 3)
        self.assertEqual((st["crossing"], ev, st["since"]), (1, None, 2))
        st, ev = B.advance(st, "RED", 4)
        self.assertEqual((st["crossing"], ev), (2, "up"))
        st, ev = B.advance(st, "AMBER", 5)
        self.assertEqual((st["crossing"], ev), (2, "down"))
        st, ev = B.advance(st, "GREEN", 6)
        self.assertEqual((st["crossing"], ev), (2, "down"))
        st, ev = B.advance(st, "RED", 7)                                  # green -> red is one crossing
        self.assertEqual((st["crossing"], ev), (3, "up"))


class Generation(TempHome):
    def setUp(self):
        super().setUp()
        self.cfg = C.load_config()

    def gen(self, band, tasks, crossing=1, cands=(), items=None, avail=ALL):
        return S.generate(self.cfg, band, crossing, tasks, list(cands), items if items is not None else D.load()["items"], avail)

    def test_green_suggests_nothing(self):
        self.assertEqual(self.gen("GREEN", [T("a")]), [])

    def test_amber_suggests_cheapest_feasible_tier(self):
        out = self.gen("AMBER", [T("daily report", tier0={"command": ["x"]}, tier=3), T("summarise chats")])
        by = {s["task"]: s for s in out}
        self.assertEqual(by["daily report"]["to_tier"], 0)
        self.assertEqual(by["daily report"]["text"], "Move 'daily report' to tier 0 (script)")
        self.assertEqual(by["summarise chats"]["to_tier"], 1)
        self.assertEqual(by["summarise chats"]["text"], "Run 'summarise chats' on tier 1 (local model) instead of Claude")

    def test_falls_to_tier2_when_no_local_model(self):
        no_ollama = lambda tier, t, cfg: (tier == 2, "")
        out = self.gen("RED", [T("summarise chats")], avail=no_ollama)
        self.assertEqual(out[0]["text"], "Run 'summarise chats' on tier 2 (cheap) instead of Claude")

    def test_nothing_when_no_cheaper_tier_is_available(self):
        self.assertEqual(self.gen("AMBER", [T("a")], avail=NONE), [])

    def test_already_cheap_tasks_not_suggested(self):
        self.assertEqual(self.gen("AMBER", [T("a", tier=1), T("b", tier0={"command": ["x"]})]), [])

    def test_never_cheap_is_never_suggested(self):
        t = T("release notes", never_cheap=True, tier0={"command": ["x"]})
        self.assertEqual(self.gen("RED", [t]), [])
        self.assertIsNone(S.best_target(t, self.cfg, ALL))

    def test_script_candidates_from_scriptify(self):
        cands = [{"id": "s-1", "name": "dl-report", "command": "python3 r.py", "count": 9, "sessions": 5, "days": 4, "risky": False},
                 {"id": "s-2", "name": "ship", "command": "node release.js deploy", "count": 9, "sessions": 5, "days": 4, "risky": True}]
        out = self.gen("AMBER", [], cands=cands)
        self.assertEqual([s["id"] for s in out], ["s-1"])                  # risky commands are never offered
        self.assertTrue(out[0]["text"].startswith("Move 'dl-report' to tier 0 (script)"))

    def test_script_suggestions_are_capped(self):
        cands = [{"id": f"s-{i}", "name": f"n{i}", "command": "python3 r.py", "count": 3, "sessions": 2, "days": 2, "risky": False} for i in range(8)]
        self.assertEqual(len(self.gen("AMBER", [], cands=cands)), self.cfg["max_script_suggestions"])


class DeclineOnceVsNever(TempHome):
    def setUp(self):
        super().setUp()
        self.cfg = C.load_config()
        self.tasks = [T("a")]

    def tick(self):
        self._t = getattr(self, "_t", 1000) + 1
        return self._t

    def refresh(self, five_hour, **kw):
        return S.refresh(self.cfg, now=self.tick(), usage={"five_hour": five_hour}, tasks=self.tasks, candidates=[],
                         avail=ALL, **kw)

    def ids(self, doc):
        return [s["id"] for s in doc["suggestions"]]

    def test_decline_once_returns_next_crossing_only(self):
        self.assertEqual(self.ids(self.refresh(85)), ["t-a"])
        S.decline("t-a")
        self.assertEqual(self.ids(self.refresh(85)), [])                  # same band: stays quiet
        self.assertEqual(self.ids(self.refresh(100)), ["t-a"])             # amber -> red is a new crossing: asked again
        S.decline("t-a")
        self.assertEqual(self.ids(self.refresh(40)), [])                  # green
        self.assertEqual(self.ids(self.refresh(85)), ["t-a"])             # green -> amber: asked again

    def test_never_is_never(self):
        self.refresh(85)
        S.decline("t-a", never=True)
        self.assertEqual(D.load()["items"]["t-a"]["status"], "never")
        for fh in (100, 40, 85, 100):
            self.assertEqual(self.ids(self.refresh(fh)), [])

    def test_undo_brings_it_back(self):
        self.refresh(85)
        S.decline("t-a", never=True)
        S.undo("t-a")
        self.assertEqual(self.ids(self.refresh(85)), ["t-a"])

    def test_accepted_is_not_suggested_again_and_ladder_obeys(self):
        self.refresh(85)
        S.accept("t-a")
        self.assertEqual(LD.effective_start(self.tasks[0]), 1)
        self.assertEqual(self.ids(self.refresh(100)), [])
        S.undo("t-a")
        self.assertEqual(LD.effective_start(self.tasks[0]), 3)

    def test_unknown_id_is_an_error(self):
        with self.assertRaises(KeyError):
            S.accept("t-zzz")

    def test_id_prefix_works_when_unique(self):
        self.refresh(85)
        S.decline("t-")
        self.assertEqual(D.load()["items"]["t-a"]["status"], "declined")

    def test_script_accept_writes_draft_and_schedules_nothing(self):
        cands = [{"id": "s-1", "name": "dl-report", "command": "python3 r.py", "count": 9, "sessions": 5, "days": 4, "risky": False}]
        S.refresh(self.cfg, now=5, usage={"five_hour": 85}, tasks=[], candidates=cands, avail=ALL)
        note = S.accept("s-1")
        draft = os.path.join(self.lf, "drafts", "dl-report.sh")
        self.assertIn("Draft", note)
        self.assertIn("localjob add dl-report", open(draft).read())
        self.assertFalse(os.path.exists(os.path.join(self.lf, "jobs")))
        self.assertFalse(os.path.exists(os.path.join(self.home, "Library")))


class AutoMove(TempHome):
    def test_default_is_off(self):
        self.assertIs(C.DEFAULTS["auto_move"], False)
        self.assertIs(C.load_config()["auto_move"], False)

    def test_off_means_nothing_moves_by_itself(self):
        cfg = C.load_config()
        t = T("a")
        doc = S.refresh(cfg, now=1, usage={"five_hour": 100}, tasks=[t], candidates=[], avail=ALL)
        self.assertEqual(len(doc["suggestions"]), 1)
        self.assertEqual(D.load()["items"], {})
        self.assertEqual(LD.effective_start(t), 3)

    def test_on_applies_moves_and_marks_them_auto(self):
        self.write(os.path.join(self.lf, "config.json"), json.dumps({"auto_move": True}))
        cfg = C.load_config()
        self.assertTrue(cfg["auto_move"])
        t = T("a")
        S.refresh(cfg, now=1, usage={"five_hour": 100}, tasks=[t], candidates=[{"id": "s-1", "name": "x", "command": "python3 r.py",
                  "count": 3, "sessions": 2, "days": 2, "risky": False}], avail=ALL)
        item = D.load()["items"]["t-a"]
        self.assertEqual((item["status"], item["auto"], item["to_tier"]), ("accepted", True, 1))
        self.assertNotIn("s-1", D.load()["items"])                         # script drafts are never auto-accepted
        self.assertEqual(LD.effective_start(t), 1)

    def test_auto_move_still_respects_decline_never_and_never_cheap(self):
        self.write(os.path.join(self.lf, "config.json"), json.dumps({"auto_move": True}))
        cfg = C.load_config()
        D.record("t-n", "never", 0, task="n")
        D.record("t-d", "declined", 1, task="d")
        tasks = [T("n"), T("d"), T("c", never_cheap=True), T("ok")]
        S.refresh(cfg, now=1, usage={"five_hour": 100}, tasks=tasks, candidates=[], avail=ALL)
        # band was green before this refresh, so this is crossing 1 and "declined at crossing 1" stays hidden
        items = D.load()["items"]
        self.assertEqual(items["t-n"]["status"], "never")
        self.assertEqual(items["t-d"]["status"], "declined")
        self.assertNotIn("t-c", items)
        self.assertEqual(items["t-ok"]["status"], "accepted")

    def test_green_never_auto_moves(self):
        self.write(os.path.join(self.lf, "config.json"), json.dumps({"auto_move": True}))
        S.refresh(C.load_config(), now=1, usage={"five_hour": 10}, tasks=[T("a")], candidates=[], avail=ALL)
        self.assertEqual(D.load()["items"], {})


class StatusAndCli(TempHome):
    def test_status_and_cli(self):
        cfg = C.load_config()
        S.refresh(cfg, now=1, usage={"five_hour": 85, "estimate": True}, tasks=[T("a"), T("b")], candidates=[], avail=ALL)
        S.accept("t-a")
        S.decline("t-b", never=True)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["status"]), 0)
        text = out.getvalue()
        self.assertIn("Band: AMBER", text)
        self.assertIn("'a' -> tier 1 (local model)", text)
        self.assertIn("Never suggest: 1", text)
        self.assertIn("auto_move: off", text)
        self.assertIn("ESTIMATE", text)

    def test_cli_suggestions_accept_decline(self):
        S.refresh(C.load_config(), now=1, usage={"five_hour": 85}, tasks=[T("a"), T("b")], candidates=[], avail=ALL)
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["suggestions"])
            cli.main(["accept", "t-a"])
            cli.main(["decline", "t-b", "--never"])
            cli.main(["suggestions"])
        text = out.getvalue()
        self.assertIn("t-a", text.split("localfirst accept")[0])
        self.assertTrue(text.rstrip().endswith("No suggestions right now."))
        self.assertEqual(D.load()["items"]["t-b"]["status"], "never")

    def test_decisions_file_location(self):
        S.refresh(C.load_config(), now=1, usage={"five_hour": 85}, tasks=[T("a")], candidates=[], avail=ALL)
        S.decline("t-a")
        self.assertTrue(os.path.exists(os.path.join(self.lf, "decisions.json")))

    def test_ladder_prints_pending_suggestion_hint(self):
        S.refresh(C.load_config(), now=1, usage={"five_hour": 85}, tasks=[T("a")], candidates=[], avail=ALL)
        self.assertEqual(LD.pending_hint("a")["id"], "t-a")
        self.assertIsNone(LD.pending_hint("zzz"))


if __name__ == "__main__":
    import unittest
    unittest.main()
