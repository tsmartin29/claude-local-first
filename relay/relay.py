#!/usr/bin/env python3
"""relay (tier 2 trigger): when your Claude usage runs out, hand routine work to a cheaper model and keep going.

Run it every 10 minutes with `localjob add relay --every 10m -- python3 <path>/relay.py`. No AI in this script itself. Each run it:
 1. writes a short digest of your recent Claude Code chats (read from local transcripts; nothing is sent anywhere);
 2. decides if Claude usage is EMPTY: the usage file (see meter/) is at its cap, OR a chat hit a usage-limit
    error in the last 20 minutes and no chat got a normal reply in the last 10;
 3. ONLY when EMPTY, auto_run is on (your opt-in) and the Mac has spare RAM, swap and disk, starts ONE cheap-mode run (claude-cheap, via OpenRouter)
    that summarises chats into your inbox file and works through queued handoff files. At most one run at a time, at most
    one per cooldown;
 4. notifies you when usage goes empty and when it is back, and once a day moves old relay files to the Trash.

Config: $RELAY_CONFIG or $RELAY_HOME/config.json (default home ~/.local-first/relay). See relay/config.example.json.
Usage: relay.py [--status]   (--status prints what it sees and exits, changing nothing)
"""
import datetime as dt, glob, json, os, shlex, shutil, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from local_first import common as C   # shared RAM / disk / swap gate

HOME = os.path.expanduser("~")
LF_HOME = C.lf_home()
RELAY_HOME = os.path.expanduser(os.environ.get("RELAY_HOME", os.path.join(LF_HOME, "relay")))
CONFIG_FILE = os.path.expanduser(os.environ.get("RELAY_CONFIG", os.path.join(RELAY_HOME, "config.json")))
GB = 1024 ** 3
DEFAULTS = {
    "workdir": "~",                                         # where the cheap model works (it can only edit files here)
    "inbox": "~/.local-first/INBOX.md",                      # results land here (appended, never overwritten); shared with localjob
    "queue_dir": "~/.local-first/queue",                     # handoff .md files: one task per file
    "usage_file": "~/.local-first/usage.json",           # written by meter/usage_from_transcripts.py, or by you
    "transcripts_dir": "~/.claude/projects",                # Claude Code's local session logs
    "cooldown_seconds": 3600,
    "check_ram": True, "min_avail_gb": 6, "max_swap_gb": 6, "min_disk_gb": 6.5,
    "notify": True,
    # You stay in control: by default relay only TELLS you usage is empty. It starts the cheap run itself only if you set
    # "auto_run": true here, or "auto_move": true in ~/.local-first/config.json.
    "auto_run": False,
    # command that does the cheap run; "{prompt}" is replaced by the prompt text. Prefix with ["heavyrun", "relay"] to use memguard's queue (github.com/tsmartin29/memguard).
    "wrapper": [],
    "cheap_command": ["claude-cheap", "haiku", "-p", "{prompt}", "--permission-mode", "acceptEdits",
                      "--allowedTools", "Read Glob Grep Write Edit LS"],
    "limit_phrases": ["usage limit", "limit reached", "hit your limit", "out of extra usage", "rate_limit", "credit"],
}

def load_config():
    cfg = dict(DEFAULTS)
    try: cfg.update(json.load(open(CONFIG_FILE)))
    except FileNotFoundError: pass
    cfg["auto_run"] = bool(cfg["auto_run"] or C.load_config().get("auto_move"))
    for k in ("workdir", "inbox", "queue_dir", "usage_file", "transcripts_dir"):
        if cfg[k].startswith("~/.local-first"): cfg[k] = LF_HOME + cfg[k][len("~/.local-first"):]
        cfg[k] = os.path.expanduser(cfg[k])
    return cfg

def load(p, d):
    try: return json.load(open(p))
    except Exception: return d

def notify(cfg, msg):
    if cfg["notify"] and "PYTEST" not in os.environ and "RELAY_NO_NOTIFY" not in os.environ:
        subprocess.run(["osascript", "-e", f'display notification "{msg.replace(chr(34), chr(39))}" with title "Claude relay"'], capture_output=True)

def tail_lines(path, n_bytes=400_000):
    with open(path, "rb") as fh:
        fh.seek(max(0, os.path.getsize(path) - n_bytes)); return fh.read().decode("utf-8", "ignore").splitlines()

def recent_transcripts(cfg, now, seconds):
    return [f for f in glob.glob(os.path.join(cfg["transcripts_dir"], "**", "*.jsonl"), recursive=True) if now - os.path.getmtime(f) <= seconds]

def write_digest(cfg, now, out):
    """One block per chat touched in the last 6 hours: folder, last user message, last assistant text. Local file only."""
    blocks = []
    for f in sorted(recent_transcripts(cfg, now, 6 * 3600), key=os.path.getmtime, reverse=True)[:15]:
        last = {"user": "", "assistant": ""}; cwd = ""
        for line in tail_lines(f):
            try: d = json.loads(line)
            except ValueError: continue
            cwd = d.get("cwd") or cwd
            role = d.get("type")
            if role in last:
                m = d.get("message")
                c = m.get("content") if isinstance(m, dict) else m
                text = c if isinstance(c, str) else " ".join(b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
                if text.strip(): last[role] = " ".join(text.split())[:400]
        blocks.append(f"## {os.path.basename(f)[:8]} · {cwd}\n- last user message: {last['user']}\n- last reply: {last['assistant']}\n")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "w").write(f"# Chat digest {dt.datetime.fromtimestamp(now):%Y-%m-%d %H:%M}\n\n" + "\n".join(blocks))

def usage_reasons(cfg, now):
    reasons = []
    u = load(cfg["usage_file"], {})
    if u:
        lim, spent = u.get("extra_limit") or 0, u.get("extra_spent") or 0
        extra_left = bool(lim) and spent < lim
        if (u.get("five_hour") or 0) >= 100 and not extra_left: reasons.append("5-hour window is full")
        if (u.get("weekly") or 0) >= 100 and not extra_left: reasons.append("weekly limit used up")
    for f in recent_transcripts(cfg, now, 20 * 60):
        for line in tail_lines(f)[-60:]:
            if '"isApiErrorMessage":true' in line and any(k in line.lower() for k in cfg["limit_phrases"]):
                reasons.append("a chat hit a usage limit in the last 20 minutes"); break
    # One chat or one model hitting its own limit does not mean usage is empty: a normal reply in the last 10 minutes wins.
    last_ok = 0
    for f in recent_transcripts(cfg, now, 10 * 60):
        for line in reversed(tail_lines(f)[-80:]):
            if '"type":"assistant"' in line and '"isApiErrorMessage":true' not in line:
                try: last_ok = max(last_ok, dt.datetime.fromisoformat(json.loads(line)["timestamp"].replace("Z", "+00:00")).timestamp()); break
                except Exception: continue
    if now - last_ok < 10 * 60: reasons = []
    return reasons

def ram_ok(cfg):
    """(ok, why). Uses the shared gate in local_first.common: available memory, swap and free disk."""
    if not cfg["check_ram"]: return True, ""
    return C.gate_check(cfg["min_avail_gb"], cfg["min_disk_gb"], cfg["max_swap_gb"])

def build_prompt(cfg, digest):
    return (f"Claude usage is EMPTY, so you (cheap mode) are covering until it is back. Work in {cfg['workdir']}. Steps: "
        f"(1) Read {digest} (recent chats: folder, last messages). Append ONE entry to {cfg['inbox']}: '## <date time> · chat summary' "
        "then one short bullet per chat: what it was doing, where it stopped, what it needs next. Never delete inbox entries. "
        f"(2) For each .md file in {cfg['queue_dir']} (oldest first, at most 3): do exactly what it says if it is routine work "
        "(copy, docs, reports, research, sorting files); skip anything that builds, signs, releases, deploys, publishes, spends money "
        "or touches production code, and say it was skipped. Move each finished file into the 'done' subfolder and add a short result "
        "entry to the inbox. Reply with one line.")

def start_cheap_run(cfg, lock, log, digest):
    args = [a.replace("{prompt}", build_prompt(cfg, digest)) for a in cfg["cheap_command"]]
    cmd = shlex.join(cfg["wrapper"] + args)
    done = "osascript -e 'display notification \"Cheap mode finished a cover run. See your inbox.\" with title \"Claude relay\"'" if cfg["notify"] else "true"
    sh = f'cd {shlex.quote(cfg["workdir"])} && nice -n 15 {cmd} > {shlex.quote(log)} 2>&1; rm -f {shlex.quote(lock)}; {done}'
    return subprocess.Popen(["/bin/sh", "-c", sh], start_new_session=True)

def housekeeping(cfg, now, relay_logs):
    """Once a day: old finished handoffs go to the Trash (never deleted); logs are trimmed in place."""
    moved, week = 0, 7 * 86400
    trash = os.path.join(HOME, ".Trash"); os.makedirs(trash, exist_ok=True)
    for f in glob.glob(os.path.join(cfg["queue_dir"], "done", "*.md")):
        if os.path.isfile(f) and now - os.path.getmtime(f) > week:
            shutil.move(f, os.path.join(trash, f"{os.path.basename(f)}.{int(now)}")); moved += 1
    for log in relay_logs:
        if os.path.exists(log) and os.path.getsize(log) > 2_000_000:
            data = open(log, "rb").read()[-500_000:]; open(log, "wb").write(data)
    return moved

def run(cfg, now=None, dry=False):
    now = now or time.time()
    os.makedirs(RELAY_HOME, exist_ok=True)
    state_f, lock = os.path.join(RELAY_HOME, "state.json"), os.path.join(RELAY_HOME, "cheap.lock")
    digest, cheap_log = os.path.join(RELAY_HOME, "digest.md"), os.path.join(RELAY_HOME, "cheap_last.log")
    st = load(state_f, {"status": "AVAILABLE", "since": now, "last_cheap": 0})
    reasons = usage_reasons(cfg, now); empty = bool(reasons); prev = st.get("status")
    ram, why = ram_ok(cfg)
    if dry:
        return {"status": "EMPTY" if empty else "AVAILABLE", "reasons": reasons, "ram_ok": ram, "ram_note": why}
    if not os.path.exists(digest) or now - os.path.getmtime(digest) > 600 or empty: write_digest(cfg, now, digest)
    if empty and prev != "EMPTY":
        st.update(status="EMPTY", since=now, reason="; ".join(sorted(set(reasons))))
        notify(cfg, "Claude usage is empty. Cheap mode is taking over routine work." if cfg["auto_run"] else
               "Claude usage is empty. Open cheap mode (claude-cheap) for routine work, or set auto_run to let relay do it.")
    elif not empty and prev == "EMPTY":
        st.update(status="AVAILABLE", since=now, reason="")
        notify(cfg, "Claude usage is back. Cheap-mode results are waiting in your inbox.")
        os.makedirs(os.path.dirname(cfg["inbox"]), exist_ok=True)
        with open(cfg["inbox"], "a") as fh: fh.write(f"## {dt.datetime.now():%B %d, %Y %H:%M} · relay\n\nClaude usage is back. Read the entries above first.\n\n")
    running = os.path.exists(lock) and now - os.path.getmtime(lock) < 3 * 3600
    if st["status"] == "EMPTY" and not ram:
        st["ram_hold"] = why
        if st.get("ram_hold_notified", 0) < now - 3600:
            st["ram_hold_notified"] = now; notify(cfg, f"Cheap mode is waiting: {why}.")
    if st["status"] == "EMPTY" and cfg["auto_run"] and ram and not running and now - st.get("last_cheap", 0) > cfg["cooldown_seconds"]:
        st["last_cheap"] = now; open(lock, "w").write(str(now))
        for d in (cfg["queue_dir"], os.path.join(cfg["queue_dir"], "done")): os.makedirs(d, exist_ok=True)
        start_cheap_run(cfg, lock, cheap_log, digest)
    if now - st.get("last_cleanup", 0) > 86400:
        st["last_cleanup"] = now
        try: st["last_cleanup_moved"] = housekeeping(cfg, now, [os.path.join(RELAY_HOME, "relay.log"), cheap_log])
        except Exception as ex: st["last_cleanup_error"] = str(ex)
    json.dump(st, open(state_f, "w"))
    return st

if __name__ == "__main__":
    cfg = load_config()
    if "--status" in sys.argv: print(json.dumps(run(cfg, dry=True), indent=2)); sys.exit(0)
    r = run(cfg); print(r["status"], r.get("reason", ""))
