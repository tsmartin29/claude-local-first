"""Suggestions: when usage crosses into AMBER or RED, offer to move recurring work to a cheaper tier.

NOTHING moves on its own. A suggestion becomes a move only when you accept that specific one (`localfirst accept <id>`,
or the menu-bar Accept button) or when you set `auto_move: true` in config.json (default false). Decline and Never are
remembered in decisions.json; `never_cheap` tasks are never suggested for anything cheaper than Claude.
"""
import importlib.util
import os
import shlex
import time

from . import bands as B, common as C, decisions as D, ladder, scriptify

TIER_WORDS = ladder.TIER_NAMES


def suggestions_path():
    return os.path.join(C.lf_home(), "suggestions.json")


def band_path():
    return os.path.join(C.lf_home(), "band.json")


def candidates_path():
    return os.path.join(C.lf_home(), "candidates.json")


def pending():
    return (C.read_json(suggestions_path(), {}) or {}).get("suggestions", [])


# ------------------------------------------------------------------ generation
def best_target(task, cfg, avail):
    """Lowest feasible tier below where the task runs today, or None. never_cheap tasks never get one."""
    if task["never_cheap"]:
        return None
    home = ladder.effective_start(task)
    for tier in (0, 1, 2):
        if tier >= home:
            break
        if tier == 0 and not task["tier0"]:
            continue
        if tier in (1, 2):
            if not task["prompt"]:
                continue
            ok, _ = avail(tier, task, cfg)
            if not ok:
                continue
        return tier
    return None


def move_text(task, to_tier):
    name = task["name"]
    home = ladder.effective_start(task)
    if to_tier == 0:
        return f"Move '{name}' to tier 0 (script)"
    src = "Claude" if home == 3 else TIER_WORDS[home]
    return f"Run '{name}' on {TIER_WORDS[to_tier]} instead of {src}"


def generate(cfg, band, crossing, tasks, candidates, items, avail=None):
    """The suggestions to show right now. Pure: pass in the loaded tasks, scriptify candidates and decisions."""
    if band == "GREEN":
        return []
    avail = avail or ladder.available
    out = []
    for task in tasks:
        sid = "t-" + task["name"]
        if not D.suggestable(items.get(sid), crossing):
            continue
        to = best_target(task, cfg, avail)
        if to is None:
            continue
        out.append({"id": sid, "kind": "move_task", "task": task["name"], "to_tier": to, "text": move_text(task, to)})
    n = 0
    for c in candidates:
        if n >= cfg["max_script_suggestions"]:
            break
        if c.get("risky") or not D.suggestable(items.get(c["id"]), crossing):
            continue
        out.append({"id": c["id"], "kind": "script", "to_tier": 0, "name": c["name"], "command": c["command"],
                    "text": f"Move '{c['name']}' to tier 0 (script) - you ran it {c['count']}x in {c['sessions']} sessions"})
        n += 1
    return out


# ------------------------------------------------------------------ usage + candidates
def estimate_usage(cfg, now=None):
    """Run meter/usage_from_transcripts.py (estimate from local logs) and write the usage file."""
    here = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    spec = importlib.util.spec_from_file_location("usage_from_transcripts", os.path.join(here, "meter", "usage_from_transcripts.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    data = mod.compute(cfg, now)
    C.write_json(cfg["usage_file"], data)
    return data


def cached_candidates(cfg, now, scan_fn=None):
    cache = C.read_json(candidates_path(), {})
    if cache.get("scanned_at") and now - cache["scanned_at"] < cfg["scan_every_hours"] * 3600:
        return cache.get("candidates", [])
    scan_fn = scan_fn or (lambda: scriptify.scan(cfg["projects_dir"], min_count=3, since_days=60)[0])
    try:
        cands = scan_fn()
    except Exception:
        cands = cache.get("candidates", [])
    C.write_json(candidates_path(), {"scanned_at": now, "candidates": cands})
    return cands


def refresh(cfg=None, now=None, usage=None, tasks=None, candidates=None, avail=None, estimate=False):
    """Recompute band, crossing and the pending suggestions; write band.json and suggestions.json. Returns the suggestions doc."""
    cfg = cfg or C.load_config()
    now = now or time.time()
    if usage is None:
        usage = estimate_usage(cfg, now) if estimate or cfg["usage_source"] == "transcripts" else C.read_json(cfg["usage_file"], {})
    band, reasons = B.compute_band(usage, cfg["bands"])
    state, event = B.advance(C.read_json(band_path()), band, now)
    state["reasons"] = reasons
    state["estimate"] = bool((usage or {}).get("estimate"))
    C.write_json(band_path(), state)
    tasks = ladder.list_tasks(cfg["tasks_dir"]) if tasks is None else tasks
    if candidates is None:
        candidates = cached_candidates(cfg, now) if band != "GREEN" else []
    items = D.load()["items"]
    sugg = generate(cfg, band, state["crossing"], tasks, candidates, items, avail)
    if cfg["auto_move"]:                       # opt-in only; accepted moves are recorded like any other, with auto=True
        for s in list(sugg):
            if s["kind"] == "move_task":
                D.record(s["id"], "accepted", state["crossing"], now=now, task=s["task"], to_tier=s["to_tier"], auto=True)
                sugg.remove(s)
    doc = {"band": band, "reasons": reasons, "crossing": state["crossing"], "event": event, "updated": int(now), "suggestions": sugg}
    C.write_json(suggestions_path(), doc)
    return doc


# ------------------------------------------------------------------ user decisions
def find(item_id):
    hits = [s for s in pending() if s["id"] == item_id] or [s for s in pending() if s["id"].startswith(item_id)]
    if len(hits) != 1:
        raise KeyError(f"no single suggestion matches '{item_id}'. Run: localfirst suggestions")
    return hits[0]


def _drop_from_pending(item_id):
    doc = C.read_json(suggestions_path(), {}) or {}
    doc["suggestions"] = [s for s in doc.get("suggestions", []) if s["id"] != item_id]
    C.write_json(suggestions_path(), doc)


def crossing_now():
    return (C.read_json(band_path(), {}) or {}).get("crossing", 0)


def accept(item_id, now=None):
    """Record a yes. A task move takes effect on the next `ladder run`; a script suggestion writes a DRAFT you review."""
    s = find(item_id)
    note = ""
    if s["kind"] == "move_task":
        D.record(s["id"], "accepted", crossing_now(), now=now, task=s["task"], to_tier=s["to_tier"])
        note = f"'{s['task']}' will start on {TIER_WORDS[s['to_tier']]} from now on. Undo: localfirst undo {s['id']}"
    else:
        draft_dir = C.ensure(os.path.join(C.lf_home(), "drafts"))
        path = os.path.join(draft_dir, s["name"] + ".sh")
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n# DRAFT from scriptify. Review the command and pick a schedule, then run this file.\n"
                     "# Keep the script and its data outside ~/Desktop, ~/Documents and ~/Downloads (macOS privacy).\n"
                     f"localjob add {s['name']} --every 2h -- sh -c {shlex.quote(s['command'])}\n")
        D.record(s["id"], "accepted", crossing_now(), now=now, kind="script", draft=path)
        note = f"Draft written: {path}  (nothing is scheduled until you review it and run it)"
    _drop_from_pending(s["id"])
    return note


def decline(item_id, never=False, now=None):
    s = find(item_id)
    D.record(s["id"], "never" if never else "declined", crossing_now(), now=now, task=s.get("task"))
    _drop_from_pending(s["id"])
    return ("Won't suggest this again. Undo: localfirst undo " + s["id"]) if never else "Okay. It may come back the next time usage rises into a higher band."


def undo(item_id):
    gone = D.forget(item_id)
    if not gone:
        raise KeyError(f"no saved decision called '{item_id}'")
    return f"Forgot your decision on {item_id} (it was: {gone['status']})."


# ------------------------------------------------------------------ status
def status(cfg=None):
    cfg = cfg or C.load_config()
    st = C.read_json(band_path(), {}) or {}
    items = D.load()["items"]
    stats = ladder.summarize_stats(ladder.read_stats())
    return {"band": st.get("band", "GREEN"), "reasons": st.get("reasons", []), "estimate": st.get("estimate", False),
            "crossing": st.get("crossing", 0), "pending": pending(), "auto_move": cfg["auto_move"],
            "accepted": {k: v for k, v in items.items() if v["status"] == "accepted"},
            "declined": [k for k, v in items.items() if v["status"] == "declined"],
            "never": [k for k, v in items.items() if v["status"] == "never"], "stats": stats}
