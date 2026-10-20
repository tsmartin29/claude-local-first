#!/usr/bin/env python3
"""scriptify: find shell commands you keep asking Claude Code to run, so you can turn them into scheduled scripts (tier 0).

Reads your own Claude Code transcripts (~/.claude/projects/*/*.jsonl) READ-ONLY. Nothing is uploaded: this module imports
no networking code at all. Anything that looks like a secret is redacted before it is printed or stored.

  scriptify [--root DIR] [--min-count 3] [--top 15] [--since-days 60] [--json]
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shlex
import sys
from collections import Counter, defaultdict
from datetime import datetime

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    from local_first import common as C
else:
    from . import common as C

# one-off navigation / inspection commands that are never worth scheduling
TRIVIAL = {"ls", "cd", "pwd", "echo", "cat", "which", "date", "whoami", "true", "clear", "head", "tail", "wc", "open", "mkdir",
           "touch", "sleep", "printf", "test", "[", "grep", "sed", "rg", "awk", "find", "sort", "diff", "stat", "file", "tree",
           "uniq", "cut", "tr", "xargs", "ps", "kill", "pkill", "lsof", "tee", "mv", "cp", "ln", "chmod", "source", "export"}
GIT_SKIP = True
WRAPPERS = {"sudo", "nice", "time", "env", "caffeinate", "exec"}


def head_word(cmd):
    """First meaningful program in a command: skips `cd x &&`, VAR=1 prefixes and wrappers like nice/sudo."""
    last = re.split(r"&&|;|\|\|", cmd.strip())
    for seg in last:
        try:
            toks = shlex.split(seg)
        except ValueError:
            toks = seg.split()
        toks = [t for t in toks if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t)]
        while toks and toks[0] in WRAPPERS:
            toks = toks[1:]
        if not toks or toks[0] in ("cd", "export", "set"):
            continue
        return os.path.basename(toks[0])
    return ""


def normalize(cmd):
    """Key used to decide two commands are 'the same': redacted, whitespace collapsed, dates and numbers masked."""
    c = " ".join(C.redact(cmd).split())
    c = re.sub(r"\d{4}-\d{2}-\d{2}", "DATE", c)
    return re.sub(r"\d+", "N", c)


def is_candidate(cmd, include_git=False):
    if not cmd or len(cmd.strip()) < 12 or "<<" in cmd or "\n" in cmd.strip():
        return False
    hw = head_word(cmd)
    if not hw or hw in TRIVIAL:
        return False
    if hw == "git" and not include_git:
        return False
    return True


def iter_events(root, since_ts=None):
    """Yield (session_id, day, tool_name, command_or_None) for every tool call in every transcript. Read-only."""
    for path in sorted(glob.glob(os.path.join(os.path.expanduser(root), "*", "*.jsonl"))):
        try:
            mtime = os.path.getmtime(path)
            if since_ts and mtime < since_ts:
                continue
            sid = os.path.basename(path)[:-6]
            day_fallback = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if '"tool_use"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("type") != "assistant":
                        continue
                    content = (d.get("message") or {}).get("content")
                    if not isinstance(content, list):
                        continue
                    day = str(d.get("timestamp") or day_fallback)[:10]
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            inp = b.get("input") or {}
                            cmd = inp.get("command") if b.get("name") == "Bash" and isinstance(inp, dict) else None
                            yield sid, day, b.get("name", "?"), cmd
        except OSError:
            continue


def analyse(events, min_count=3, include_git=False, seq_len=3, min_seq_sessions=3):
    cmds = defaultdict(lambda: {"count": 0, "sessions": set(), "days": set(), "variants": Counter()})
    seqs = defaultdict(set)
    per_session = defaultdict(list)
    for sid, day, tool, cmd in events:
        if cmd is None:
            per_session[sid].append(None)          # a non-Bash tool breaks a run of shell steps
            continue
        if not is_candidate(cmd, include_git):
            per_session[sid].append(None)
            continue
        key = normalize(cmd)
        e = cmds[key]
        e["count"] += 1
        e["sessions"].add(sid)
        e["days"].add(day)
        e["variants"][" ".join(C.redact(cmd).split())] += 1
        per_session[sid].append(head_word(cmd))
    cands = []
    for key, e in cmds.items():
        if e["count"] >= min_count and (len(e["sessions"]) >= 2 or len(e["days"]) >= 2):
            text = e["variants"].most_common(1)[0][0]
            cands.append({"id": "s-" + hashlib.sha1(key.encode()).hexdigest()[:8], "command": text, "count": e["count"],
                          "sessions": len(e["sessions"]), "days": len(e["days"]), "name": suggest_name(text),
                          "risky": risky(text)})
    cands.sort(key=lambda c: (c["days"], c["sessions"], c["count"]), reverse=True)
    for sid, run in per_session.items():
        for i in range(len(run) - seq_len + 1):
            win = run[i:i + seq_len]
            if None not in win and len(set(win)) > 1:
                seqs[tuple(win)].add(sid)
    sequences = sorted(({"steps": list(k), "sessions": len(v)} for k, v in seqs.items() if len(v) >= min_seq_sessions),
                       key=lambda s: s["sessions"], reverse=True)
    return cands, sequences


RISKY = re.compile(r"\b(deploy|publish|release|upload|push|rm|rmdir|delete|drop|send|sign|notari[sz]e|submit|purchase|charge|refund)\b", re.I)


def risky(cmd):
    """Commands with side effects in the outside world are judgment work, not something to put on a timer."""
    return bool(RISKY.search(cmd))


def suggest_name(cmd):
    for tok in re.split(r"\s+", cmd):
        m = re.search(r"([A-Za-z][A-Za-z0-9_-]*)\.(?:py|sh|mjs|js|rb|pl)$", tok.strip("'\";|&"))
        if m:
            return m.group(1).replace("_", "-").lower()[:40]
    words = re.findall(r"[A-Za-z][A-Za-z0-9]+", head_word(cmd) + " " + " ".join(cmd.split()[1:4]))
    return "-".join(w.lower() for w in words[:2] if w.lower() not in ("redacted", "py"))[:40] or "job"


def localjob_line(c):
    return f"localjob add {c['name']} --every 2h -- sh -c {shlex.quote(c['command'])}"


def scan(root, min_count=3, since_days=None, include_git=False):
    import time
    since = time.time() - since_days * 86400 if since_days else None
    return analyse(iter_events(root, since), min_count=min_count, include_git=include_git)


def build_parser():
    ap = argparse.ArgumentParser(prog="scriptify", description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default="~/.claude/projects", help="transcripts folder (read-only)")
    ap.add_argument("--min-count", type=int, default=3, help="a command must have run at least this often")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--since-days", type=int, default=60)
    ap.add_argument("--include-git", action="store_true")
    ap.add_argument("--json", action="store_true")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    cands, seqs = scan(a.root, a.min_count, a.since_days, a.include_git)
    if a.json:
        print(json.dumps({"commands": cands[:a.top], "sequences": seqs[:a.top]}, indent=2))
        return 0
    if not cands:
        print(f"No command repeated {a.min_count}+ times across sessions or days in the last {a.since_days} days.")
    else:
        print(f"Commands you keep running ({len(cands)} found; showing {min(a.top, len(cands))}). Secret-looking text is redacted.\n")
        for c in cands[:a.top]:
            print(f"  seen {c['count']}x in {c['sessions']} sessions on {c['days']} days")
            print(f"    {c['command'][:300]}")
            if c["risky"]:
                print("    (looks like it changes things outside your machine: judgment work, not suggested for a timer)\n")
            else:
                print(f"    -> {localjob_line(c)[:360]}\n")
        print("Review each one before scheduling: a command may depend on the folder or state of the session it came from.")
    if seqs:
        print("\nShell steps that often come in the same order (a script could do all of them):")
        for s in seqs[:min(a.top, 8)]:
            print(f"  {' -> '.join(s['steps'])}   ({s['sessions']} sessions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
