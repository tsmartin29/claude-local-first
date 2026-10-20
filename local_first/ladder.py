#!/usr/bin/env python3
"""ladder: run recurring work on the cheapest tier that is allowed, available, and that YOU have approved.

  Tier 0  a local deterministic script (no model)
  Tier 1  a local model through Ollama (no API cost)
  Tier 2  a cheap API model through OpenRouter (claude-cheap)
  Tier 3  Claude. The ladder never spends Claude on its own: it writes a handoff file (or runs `claude -p` with --allow-claude)

A task file (YAML subset or JSON):

  name: morning-digest
  tier: 3                    # where it runs today (default: 0 if it has a tier0 command, otherwise 3)
  tier0: {command: [...]}    # optional script (or `command: "..."` / list)
  prompt: "Summarise these in 5 bullets."
  inputs: [notes.md]         # files appended to the prompt for tiers 1-2
  never_cheap: false         # true = always Claude: no cheaper tier is ever used or suggested

Moving a task to a cheaper tier than `tier:` happens only if you accepted that move (`localfirst accept`) or opted in with
auto_move. From the starting tier the ladder falls back UP (0 -> 1 -> 2 -> 3) when a tier fails or is unavailable.

  ladder run TASK [--allow-claude] [--dry-run]     ladder plan TASK     ladder stats
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    from local_first import common as C, decisions as D, miniyaml
else:
    from . import common as C, decisions as D, miniyaml

TIER_NAMES = {0: "tier 0 (script)", 1: "tier 1 (local model)", 2: "tier 2 (cheap)", 3: "tier 3 (Claude)"}


class TaskError(ValueError):
    pass


# ------------------------------------------------------------------ task files
def _argv(v):
    if isinstance(v, str):
        return shlex.split(v)
    if isinstance(v, list) and v and all(isinstance(x, (str, int, float)) for x in v):
        return [str(x) for x in v]
    raise TaskError("a command must be a string or a list of words")


def normalize_task(raw, base_dir, default_name="task"):
    if not isinstance(raw, dict):
        raise TaskError("a task file must contain a map of settings")
    t = {"name": str(raw.get("name") or default_name), "description": raw.get("description") or "", "prompt": raw.get("prompt") or "",
         "never_cheap": bool(raw.get("never_cheap", False)), "base_dir": base_dir, "model": raw.get("model") or "",
         "output": raw.get("output"), "max_tier": int(raw.get("max_tier", 3))}
    cmd = raw.get("tier0", raw.get("command"))
    t0 = None
    if isinstance(cmd, dict):
        t0 = {"argv": _argv(cmd.get("command")), "timeout": int(cmd.get("timeout", 300))}
    elif cmd:
        t0 = {"argv": _argv(cmd), "timeout": int(raw.get("timeout", 300))}
    t["tier0"] = t0
    ins = raw.get("inputs") or []
    t["inputs"] = [p if os.path.isabs(os.path.expanduser(p)) else os.path.join(base_dir, p) for p in map(os.path.expanduser, ins)]
    if not t0 and not t["prompt"] and not t["description"]:
        raise TaskError("a task needs a tier0 command, a prompt, or at least a description")
    if not 0 <= t["max_tier"] <= 3:
        raise TaskError("max_tier must be 0-3")
    tier = raw.get("tier")
    if t["never_cheap"]:
        if tier is not None and int(tier) < 3:
            raise TaskError("never_cheap: true conflicts with tier: %s (never_cheap tasks always run on Claude)" % tier)
        tier = 3
    t["home_tier"] = int(tier) if tier is not None else (0 if t0 else 3)
    return t


def load_task(path):
    path = os.path.expanduser(path)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    try:
        raw = json.loads(text) if path.endswith(".json") else miniyaml.loads(text)
    except ValueError as ex:
        raise TaskError(f"{path}: {ex}")
    return normalize_task(raw, os.path.dirname(os.path.abspath(path)), os.path.splitext(os.path.basename(path))[0])


def list_tasks(tasks_dir):
    out = []
    if not os.path.isdir(tasks_dir):
        return out
    for f in sorted(os.listdir(tasks_dir)):
        if f.endswith((".yaml", ".yml", ".json")):
            try:
                out.append(load_task(os.path.join(tasks_dir, f)))
            except (TaskError, OSError, ValueError):
                continue
    return out


def effective_start(task, decisions=None):
    """Where this task starts on the ladder: its own tier, or a cheaper one ONLY if you accepted that move."""
    if task["never_cheap"]:
        return 3
    to = D.accepted_tier(task["name"], decisions)
    return min(task["home_tier"], to) if to is not None else task["home_tier"]


# ------------------------------------------------------------------ tiers
def ollama_models(url, timeout=1.5):
    """Names of local models, or None when Ollama is not answering."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=timeout) as r:
            return [m.get("name") for m in json.load(r).get("models", []) if m.get("name")]
    except (urllib.error.URLError, OSError, ValueError):
        return None


def ollama_generate(url, model, prompt, timeout=300):
    body = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/api/generate", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r).get("response", "")


def available(tier, task, cfg):
    """(bool, why). Cheap checks only."""
    if tier == 0:
        return (True, "") if task["tier0"] else (False, "no tier0 command")
    if tier in (1, 2) and not task["prompt"]:
        return False, "no prompt"
    if tier == 1:
        models = ollama_models(cfg["ollama_url"])
        if models is None:
            return False, "Ollama is not running (install it from ollama.com and `ollama pull` a model)"
        return (True, "") if (models or task["model"] or cfg["ollama_model"]) else (False, "Ollama has no models pulled")
    if tier == 2:
        exe = cfg["tier2_command"][0]
        return (True, "") if shutil.which(exe) else (False, f"'{exe}' not on PATH (see bin/claude-cheap and README)")
    return True, ""


def read_inputs(task, cfg, for_api):
    parts, total, truncated = [], 0, False
    cap = int(cfg["max_input_chars"])
    for p in task["inputs"]:
        with open(p, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        total += len(text)
        if for_api and cfg["redact_for_api"]:
            text = C.redact(text)
        if len(text) > cap:
            text, truncated = text[:cap] + "\n[... truncated ...]", True
        parts.append(f"--- file: {os.path.basename(p)} ---\n{text}")
    return "\n\n".join(parts), total, truncated


def build_prompt(task, cfg, for_api):
    body, _, _ = read_inputs(task, cfg, for_api)
    return task["prompt"] + (("\n\n" + body) if body else "")


def input_chars(task, include_prompt=True):
    n = len(task["prompt"]) if include_prompt else 0
    for p in task["inputs"]:
        try:
            n += os.path.getsize(p)
        except OSError:
            pass
    return n


def run_tier0(task, cfg, ctx):
    t0 = task["tier0"]
    r = subprocess.run(t0["argv"], capture_output=True, text=True, timeout=t0["timeout"], cwd=task["base_dir"])
    if r.returncode != 0:
        raise RuntimeError(f"exit {r.returncode}: {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout


def run_tier1(task, cfg, ctx):
    model = task["model"] or cfg["ollama_model"] or (ollama_models(cfg["ollama_url"]) or [""])[0]
    if not model:
        raise RuntimeError("no Ollama model configured")
    out = ollama_generate(cfg["ollama_url"], model, build_prompt(task, cfg, for_api=False))
    if not out.strip():
        raise RuntimeError("the local model returned nothing")
    return out


def run_command_with_prompt(command, prompt, timeout):
    argv = [prompt if a == "{prompt}" else a.replace("{prompt}", prompt) for a in command]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"exit {r.returncode}: {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout


def run_tier2(task, cfg, ctx):
    return run_command_with_prompt(cfg["tier2_command"], build_prompt(task, cfg, for_api=True), 900)


def run_tier3_claude(task, cfg, ctx):
    return run_command_with_prompt(cfg["tier3_command"], build_prompt(task, cfg, for_api=True), 1800)


def write_handoff(task, cfg, attempts, reason):
    C.ensure(cfg["handoff_dir"])
    path = os.path.join(cfg["handoff_dir"], f"{task['name']}-{datetime.now():%Y%m%d-%H%M%S}.md")
    lines = [f"# Handoff: {task['name']}", "", f"Why this needs Claude: {reason}", ""]
    if task["description"]:
        lines += [task["description"], ""]
    if task["prompt"]:
        lines += ["## Task", "", task["prompt"], ""]
    if task["inputs"]:
        lines += ["## Input files", ""] + [f"- {p}" for p in task["inputs"]] + [""]
    if attempts:
        lines += ["## What was tried first", ""] + [f"- {a['name']}: {a['status']}" + (f" ({a['note']})" if a.get("note") else "") for a in attempts] + [""]
    open(path, "w").write("\n".join(lines))
    C.ensure(os.path.dirname(cfg["inbox"]))
    with open(cfg["inbox"], "a") as fh:
        fh.write(f"## {datetime.now():%B %d, %Y %H:%M} · ladder · {task['name']}\n\n"
                 f"'{task['name']}' needs Claude ({reason}). Handoff file: {path}\n\n")
    return path


RUNNERS = {0: run_tier0, 1: run_tier1, 2: run_tier2}


# ------------------------------------------------------------------ planning and running
def plan(task, cfg, decisions=None, avail=None):
    """List of {'tier','status','why'} the ladder would walk, lowest first. status: try | unavailable | blocked."""
    avail = avail or available
    start = effective_start(task, decisions)
    rows = []
    for tier in range(0, 4):
        if task["never_cheap"] and tier < 3:
            rows.append({"tier": tier, "status": "blocked", "why": "never_cheap"})
        elif tier < start:
            rows.append({"tier": tier, "status": "blocked", "why": f"starts at {TIER_NAMES[start]}; moving cheaper needs your OK (localfirst accept)"})
        elif tier > task["max_tier"]:
            rows.append({"tier": tier, "status": "blocked", "why": f"max_tier is {task['max_tier']}"})
        elif tier == 3:
            ok = bool(task["prompt"] or task["description"])
            rows.append({"tier": 3, "status": "try" if ok else "unavailable", "why": "" if ok else "nothing to hand over"})
        elif (tier == 0 and not task["tier0"]) or (tier in (1, 2) and not task["prompt"]):
            rows.append({"tier": tier, "status": "unavailable", "why": "no tier0 command" if tier == 0 else "no prompt"})
        else:
            ok, why = avail(tier, task, cfg)
            rows.append({"tier": tier, "status": "try" if ok else "unavailable", "why": why})
    return rows


def estimate_tokens(chars):
    return int(chars) // 4


def run_task(task, cfg, decisions=None, avail=None, runners=None, allow_claude=False):
    runners = {**RUNNERS, **(runners or {})}
    attempts, output, ran, status, handoff = [], "", None, "failed", None
    for row in plan(task, cfg, decisions, avail):
        t = row["tier"]
        if row["status"] == "blocked":
            continue
        if row["status"] == "unavailable":
            attempts.append({"tier": t, "name": TIER_NAMES[t], "status": "unavailable", "note": row["why"]})
            continue
        if t == 3:
            if allow_claude:
                try:
                    output = (runners.get(3) or run_tier3_claude)(task, cfg, None)
                    attempts.append({"tier": 3, "name": TIER_NAMES[3], "status": "ok"})
                    ran, status = 3, "ok"
                    break
                except Exception as ex:
                    attempts.append({"tier": 3, "name": TIER_NAMES[3], "status": "failed", "note": str(ex)[:200]})
            reason = "never_cheap" if task["never_cheap"] else ("every cheaper tier failed or was unavailable" if attempts else "it starts at Claude")
            handoff = write_handoff(task, cfg, attempts, reason)
            attempts.append({"tier": 3, "name": TIER_NAMES[3], "status": "handoff", "note": handoff})
            ran, status = 3, "handoff"
            break
        t0 = time.time()
        try:
            output = runners[t](task, cfg, None)
            attempts.append({"tier": t, "name": TIER_NAMES[t], "status": "ok", "secs": round(time.time() - t0, 2)})
            ran, status = t, "ok"
            break
        except Exception as ex:
            attempts.append({"tier": t, "name": TIER_NAMES[t], "status": "failed", "note": str(ex)[:300], "secs": round(time.time() - t0, 2)})
    # Only what is measurable: declared input files (and, for a local-model run, the prompt) that did NOT go to an API.
    saved = estimate_tokens(input_chars(task, include_prompt=(ran == 1))) if ran in (0, 1) else 0
    return {"task": task["name"], "tier_ran": ran, "status": status, "attempts": attempts, "output": output,
            "handoff": handoff, "est_tokens_not_sent": saved, "estimate": True, "ts": int(time.time())}


def stats_path():
    return os.path.join(C.lf_home(), "ladder-stats.jsonl")


def record_stats(res):
    C.ensure(C.lf_home())
    row = {k: res[k] for k in ("ts", "task", "tier_ran", "status", "est_tokens_not_sent", "estimate")}
    row["attempts"] = [{k: a.get(k) for k in ("tier", "status")} for a in res["attempts"]]
    with open(stats_path(), "a") as fh:
        fh.write(json.dumps(row) + "\n")


def read_stats():
    rows = []
    try:
        for line in open(stats_path()):
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return rows


def summarize_stats(rows):
    by_tier = {}
    for r in rows:
        by_tier[r.get("tier_ran")] = by_tier.get(r.get("tier_ran"), 0) + 1
    return {"runs": len(rows), "by_tier": by_tier, "est_tokens_not_sent": sum(r.get("est_tokens_not_sent") or 0 for r in rows)}


def fmt_n(n):
    return f"{int(n):,}"


# ------------------------------------------------------------------ CLI
def pending_hint(task_name):
    try:
        from . import suggest
    except ImportError:
        from local_first import suggest
    for s in suggest.pending():
        if s.get("task") == task_name:
            return s
    return None


def cmd_run(a):
    cfg = C.load_config()
    task = load_task(a.task)
    if a.dry_run:
        return cmd_plan(a)
    res = run_task(task, cfg, allow_claude=a.allow_claude)
    record_stats(res)
    if task["output"] and res["output"]:
        out = os.path.expanduser(task["output"])
        C.ensure(os.path.dirname(os.path.abspath(out)))
        open(out, "w").write(res["output"])
    if res["output"]:
        print(res["output"].rstrip())
    for at in res["attempts"]:
        print(f"ladder: {at['name']}: {at['status']}" + (f" - {at['note']}" if at.get("note") else ""), file=sys.stderr)
    if res["status"] == "ok":
        msg = f"ladder: '{task['name']}' ran on {TIER_NAMES[res['tier_ran']]}."
        if res["est_tokens_not_sent"]:
            msg += f" About {fmt_n(res['est_tokens_not_sent'])} input tokens were not sent to an API (estimate: characters / 4)."
        print(msg, file=sys.stderr)
    elif res["status"] == "handoff":
        print(f"ladder: '{task['name']}' needs Claude. Handoff written: {res['handoff']}", file=sys.stderr)
    else:
        print(f"ladder: '{task['name']}' failed on every allowed tier.", file=sys.stderr)
    hint = pending_hint(task["name"])
    if hint:
        print(f"ladder: suggestion: {hint['text']}  ->  localfirst accept {hint['id']}   (or: localfirst decline {hint['id']})", file=sys.stderr)
    return 0 if res["status"] in ("ok", "handoff") else 1


def cmd_plan(a):
    cfg = C.load_config()
    task = load_task(a.task)
    print(f"{task['name']}: starts at {TIER_NAMES[effective_start(task)]}" + ("  [never_cheap]" if task["never_cheap"] else ""))
    for r in plan(task, cfg):
        print(f"  {TIER_NAMES[r['tier']]:<22}{r['status']:<13}{r['why']}")
    return 0


def cmd_stats(a):
    s = summarize_stats(read_stats())
    print(f"Ladder runs: {s['runs']}")
    for t in sorted(k for k in s["by_tier"] if k is not None):
        print(f"  {TIER_NAMES[t]}: {s['by_tier'][t]}")
    print(f"Input not sent to an API: about {fmt_n(s['est_tokens_not_sent'])} tokens (ESTIMATE: characters / 4, counted only for tier 0 and 1 runs).")
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="ladder", description="Run recurring work on the cheapest approved tier.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("task")
    r.add_argument("--allow-claude", action="store_true", help="let tier 3 run `claude -p` (otherwise it only writes a handoff file)")
    r.add_argument("--dry-run", action="store_true", help="show the plan; run nothing")
    p = sub.add_parser("plan")
    p.add_argument("task")
    sub.add_parser("stats")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        return {"run": cmd_run, "plan": cmd_plan, "stats": cmd_stats}[a.cmd](a)
    except (TaskError, OSError) as ex:
        print(f"ladder: {ex}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
