#!/usr/bin/env python3
"""localjob: schedule a plain command with launchd (macOS) or a systemd user timer (Linux). No model, no tokens.

  localjob add <name> --every 2h|15m|30s|daily@06:47 [options] -- <command ...>
  localjob list
  localjob run <name> [--force]
  localjob logs <name> [-n 40]
  localjob remove <name>

Every scheduled run goes through one wrapper (`localjob _wrap <name>`, started by launchd) which:
  * skips (does not fail) when memory or disk is short, and can queue behind memguard's `heavyrun` if it is installed;
  * logs to ~/.local-first/logs/<name>.log with rotation (old logs go to the Trash);
  * sends ONE desktop notification when the command fails (at most once an hour per job);
  * appends a one-paragraph result to the INBOX.md file;
  * recognises macOS "Operation not permitted" (privacy / TCC) and prints the exact fix.
"""
import argparse
import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    from local_first import common as C
else:
    from . import common as C

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SYSTEM_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
MIN_INTERVAL = 5


# ------------------------------------------------------------------ schedule parsing
def parse_schedule(text):
    """'2h' '15m' '30s' '1d' -> interval; 'daily@06:47' -> calendar. Raises ValueError with a readable message."""
    t = (text or "").strip().lower()
    m = re.fullmatch(r"(\d+)\s*([smhd])", t)
    if m:
        secs = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
        if secs < MIN_INTERVAL:
            raise ValueError(f"interval must be at least {MIN_INTERVAL}s (got '{text}')")
        return {"kind": "interval", "seconds": secs}
    m = re.fullmatch(r"daily@(\d{1,2}):(\d{2})", t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            raise ValueError(f"'{text}' is not a valid time (use 24-hour HH:MM)")
        return {"kind": "calendar", "hour": h, "minute": mi}
    raise ValueError(f"cannot read schedule '{text}'. Use 30s, 15m, 2h, 1d or daily@06:47")


def human_schedule(s):
    if s["kind"] == "calendar":
        return f"daily at {s['hour']:02d}:{s['minute']:02d}"
    n = s["seconds"]
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if n % size == 0:
            return f"every {n // size}{unit}"
    return f"every {n}s"


# ------------------------------------------------------------------ paths
def paths(name):
    home = C.lf_home()
    return {
        "spec": os.path.join(home, "jobs", name + ".json"),
        "state": os.path.join(home, "jobs", name + ".state.json"),
        "lock": os.path.join(home, "jobs", name + ".lock"),
        "log": os.path.join(home, "logs", name + ".log"),
        "launchd_log": os.path.join(home, "logs", name + ".launchd.log"),
    }


def label_for(cfg, name):
    return f"{cfg['label_prefix']}.{name}"


def plist_path(cfg, name):
    return os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", label_for(cfg, name) + ".plist")


def systemd_dir():
    return os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")


def default_python():
    return "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable


def wrapper_script():
    """The localjob launcher launchd should run. Prefer bin/localjob next to this package."""
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    cand = os.path.join(root, "bin", "localjob")
    return cand if os.path.exists(cand) else os.path.realpath(__file__)


# ------------------------------------------------------------------ plist / unit generation (pure functions)
def build_plist(spec, cfg, python=None, wrapper=None):
    python = python or spec.get("python") or default_python()
    wrapper = wrapper or wrapper_script()
    home = os.path.expanduser("~")
    p = paths(spec["name"])
    d = {
        "Label": label_for(cfg, spec["name"]),
        "ProgramArguments": [python, wrapper, "_wrap", spec["name"]],
        "WorkingDirectory": C.lf_home(),
        "EnvironmentVariables": {"PATH": f"{home}/.local/bin:{SYSTEM_PATH}", "HOME": home},
        "StandardOutPath": p["launchd_log"],
        "StandardErrorPath": p["launchd_log"],
        "RunAtLoad": bool(spec.get("run_at_load")),
    }
    if os.environ.get("LOCAL_FIRST_HOME"):
        d["EnvironmentVariables"]["LOCAL_FIRST_HOME"] = os.environ["LOCAL_FIRST_HOME"]
    s = spec["schedule_parsed"]
    if s["kind"] == "interval":
        d["StartInterval"] = s["seconds"]
    else:
        d["StartCalendarInterval"] = {"Hour": s["hour"], "Minute": s["minute"]}
    return plistlib.dumps(d)


def build_systemd(spec, cfg, python=None, wrapper=None):
    """(service_text, timer_text) for a systemd user timer."""
    python = python or spec.get("python") or default_python()
    wrapper = wrapper or wrapper_script()
    label = label_for(cfg, spec["name"])
    service = (f"[Unit]\nDescription=localjob {spec['name']}\n\n[Service]\nType=oneshot\n"
               f"ExecStart={shlex.join([python, wrapper, '_wrap', spec['name']])}\n")
    s = spec["schedule_parsed"]
    when = (f"OnCalendar=*-*-* {s['hour']:02d}:{s['minute']:02d}:00\n" if s["kind"] == "calendar"
            else f"OnBootSec={s['seconds']}s\nOnUnitActiveSec={s['seconds']}s\n")
    timer = f"[Unit]\nDescription=timer for {label}\n\n[Timer]\n{when}Persistent=true\n\n[Install]\nWantedBy=timers.target\n"
    return service, timer


def cron_line(spec, cfg, python=None, wrapper=None):
    python = python or spec.get("python") or default_python()
    wrapper = wrapper or wrapper_script()
    s = spec["schedule_parsed"]
    cmd = shlex.join([python, wrapper, "_wrap", spec["name"]])
    if s["kind"] == "calendar":
        return f"{s['minute']} {s['hour']} * * * {cmd}"
    n = s["seconds"]
    if n % 3600 == 0 and n < 86400:
        return f"0 */{n // 3600} * * * {cmd}"
    if n % 60 == 0 and n < 3600:
        return f"*/{n // 60} * * * * {cmd}"
    if n % 86400 == 0:
        return f"0 0 */{n // 86400} * * {cmd}"
    raise ValueError("cron cannot express this interval; use launchd or systemd")


# ------------------------------------------------------------------ launchctl helpers
def _launchctl_enabled():
    return C.IS_MAC and not os.environ.get("LOCALJOB_NO_LAUNCHCTL") and shutil.which("launchctl")


def launchctl(*args):
    if not _launchctl_enabled():
        return 0, ""
    r = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def is_loaded(cfg, name):
    if not _launchctl_enabled():
        return None
    return launchctl("print", f"gui/{os.getuid()}/{label_for(cfg, name)}")[0] == 0


# ------------------------------------------------------------------ log rotation, inbox, state
def rotate_log(path, max_bytes, keep):
    """log -> log.1 -> log.2 ...; whatever would fall off the end goes to the Trash (nothing is deleted)."""
    try:
        if os.path.getsize(path) <= max_bytes:
            return False
    except OSError:
        return False
    oldest = f"{path}.{keep}"
    if os.path.exists(oldest):
        C.trash(oldest)
    for i in range(keep - 1, 0, -1):
        if os.path.exists(f"{path}.{i}"):
            os.replace(f"{path}.{i}", f"{path}.{i + 1}")
    os.replace(path, path + ".1")
    return True


def tail_text(path, n_bytes=20000, start=0):
    try:
        with open(path, "rb") as fh:
            size = os.path.getsize(path)
            fh.seek(max(start, size - n_bytes))
            return fh.read().decode("utf-8", "ignore")
    except OSError:
        return ""


def one_paragraph(name, status, secs, output, rc=None, reason=""):
    lines = [l.strip() for l in (output or "").splitlines() if l.strip()]
    gist = " / ".join(lines[-3:])
    gist = C.redact(gist)[:500]
    head = {"ok": f"{name} ran fine in {secs:.1f}s.", "failed": f"{name} FAILED (exit {rc}) after {secs:.1f}s.",
            "skipped": f"{name} was skipped: {reason}.", "busy": f"{name} was skipped: previous run still going."}[status]
    return head + (f" Output: {gist}" if gist and status in ("ok", "failed") else "")


def append_inbox(cfg, name, status, paragraph, when=None):
    when = when or datetime.now()
    C.ensure(os.path.dirname(cfg["inbox"]))
    with open(cfg["inbox"], "a") as fh:
        fh.write(f"## {when:%B %d, %Y %H:%M} · {name} · {status}\n\n{paragraph}\n\n")


def inbox_mode(spec):
    mode = spec.get("inbox", "auto")
    if mode == "auto":
        s = spec["schedule_parsed"]
        return "fail" if s["kind"] == "interval" and s["seconds"] < 3600 else "always"
    return mode


def load_spec(name):
    spec = C.read_json(paths(name)["spec"])
    if not spec:
        raise SystemExit(f"localjob: no job called '{name}'. Try: localjob list")
    return spec


# ------------------------------------------------------------------ the wrapper
def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def run_job(name, force=False, notify_fn=None, echo=False, now=None, resources=None):
    """Run one job the way launchd does. Returns {'status','rc','secs','reason'}. status: ok|failed|skipped|busy."""
    cfg = C.load_config()
    spec = load_spec(name)
    p = paths(name)
    notify_fn = notify_fn or (lambda title, msg: C.notify(title, msg, cfg["notify"]))
    C.ensure(os.path.dirname(p["log"]))
    C.ensure(os.path.dirname(p["spec"]))
    rotate_log(p["log"], int(cfg["log_max_mb"] * 1024 * 1024), int(cfg["log_keep"]))
    state = C.read_json(p["state"], {})
    stamp = lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def log(line):
        with open(p["log"], "a") as fh:
            fh.write(f"[{stamp()}] {line}\n")
        if echo:
            print(line)

    def finish(result, output=""):
        state.update(last_run=time.time(), last_status=result["status"], last_rc=result.get("rc"), last_secs=round(result["secs"], 2))
        if result["status"] in ("ok", "failed"):
            state["runs"] = state.get("runs", 0) + 1
        if result["status"] == "failed":
            state["fails"] = state.get("fails", 0) + 1
        if result["status"] in ("skipped", "busy"):
            state["skips"] = state.get("skips", 0) + 1
        C.write_json(p["state"], state)
        return result

    # one at a time
    if os.path.exists(p["lock"]):
        try:
            if _alive(int(open(p["lock"]).read().strip() or 0)):
                log("skipped: previous run still going")
                return finish({"status": "busy", "rc": None, "secs": 0, "reason": "previous run still going"})
        except ValueError:
            pass
    # resource gate
    if not force:
        ok, why = C.gate_check(spec.get("min_avail_gb", cfg["min_avail_gb"]), spec.get("min_disk_gb", cfg["min_disk_gb"]),
                               res=resources)
        if not ok:
            log(f"skipped: {why}")
            return finish({"status": "skipped", "rc": None, "secs": 0, "reason": why})

    argv = list(spec["argv"])
    if spec.get("heavy") and shutil.which("heavyrun"):
        pre = ["heavyrun", f"localjob: {name}"]
        argv = (["env", "HEAVY_LIGHT=1"] if spec["heavy"] == "light" else []) + pre + argv
    open(p["lock"], "w").write(str(os.getpid()))
    start = time.time()
    log(f"start: {shlex.join(argv)}")
    offset = os.path.getsize(p["log"])
    rc, timed_out = None, False
    env = dict(os.environ, LOCALJOB_NAME=name, LOCAL_FIRST_INBOX=cfg["inbox"])
    try:
        with open(p["log"], "ab") as out:
            try:
                proc = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                                        cwd=spec.get("cwd") or C.lf_home(), start_new_session=True)
                try:
                    rc = proc.wait(timeout=spec.get("timeout", 3600))
                except subprocess.TimeoutExpired:
                    timed_out = True
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        rc = proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        rc = proc.wait()
            except (FileNotFoundError, PermissionError) as ex:
                out.write(f"cannot start command: {ex}\n".encode())
                rc = 127
    finally:
        try:
            os.remove(p["lock"])        # our own lock file, not user data
        except OSError:
            pass
    secs = time.time() - start
    output = tail_text(p["log"], start=offset)
    status = "ok" if rc == 0 and not timed_out else "failed"
    extra = ""
    if timed_out:
        extra = f"timed out after {spec.get('timeout', 3600)}s"
    if status == "failed" and C.looks_like_tcc(output):
        fix = C.tcc_fix(spec.get("python") or default_python(), label_for(cfg, name) if C.IS_MAC else None)
        log("FAILED: macOS privacy block\n" + fix)
        output += "\n" + fix
        extra = "macOS privacy block (Full Disk Access)"
    log(f"end: {status} exit {rc} in {secs:.1f}s {extra}".strip())
    if echo and status == "failed" and C.looks_like_tcc(output):
        print(C.tcc_fix(spec.get("python") or default_python(), label_for(cfg, name) if C.IS_MAC else None))

    para = one_paragraph(name, status, secs, output if not extra else extra + ". " + output, rc)
    if status == "failed" and C.looks_like_tcc(output):
        para = (f"{name} FAILED: macOS privacy block (\"Operation not permitted\"). Keep the job's files outside Desktop/Documents/"
                f"Downloads, or give {spec.get('python') or default_python()} Full Disk Access (System Settings > Privacy & Security). "
                f"Exact steps: localjob logs {name}")
    mode = inbox_mode(spec)
    if mode == "always" or (mode == "fail" and status == "failed"):
        append_inbox(cfg, name, status, para)
    if status == "failed":
        # one notification per failure, but not more than one an hour for a job that keeps failing
        if time.time() - state.get("last_fail_notified", 0) > 3600:
            state["last_fail_notified"] = time.time()
            notify_fn(f"localjob: {name} failed", extra or f"exit {rc}. See: localjob logs {name}")
    else:
        state.pop("last_fail_notified", None)
    return finish({"status": status, "rc": rc, "secs": secs, "reason": extra})


# ------------------------------------------------------------------ commands
def cmd_add(a, command):
    cfg = C.load_config()
    if not NAME_RE.match(a.name):
        raise SystemExit("localjob: name must be lowercase letters, digits, '.', '_' or '-' (max 64)")
    if not command:
        raise SystemExit("localjob: put the command after --   e.g.  localjob add backup --every daily@03:30 -- ./backup.sh")
    try:
        sched = parse_schedule(a.every)
    except ValueError as ex:
        raise SystemExit(f"localjob: {ex}")
    spec = {"name": a.name, "argv": command, "schedule": a.every, "schedule_parsed": sched, "python": a.python or default_python(),
            "heavy": "light" if a.heavy_light else ("full" if a.heavy else None), "inbox": a.inbox,
            "timeout": a.timeout, "run_at_load": a.run_at_load, "created": int(time.time())}
    if a.min_avail_gb is not None:
        spec["min_avail_gb"] = a.min_avail_gb
    if a.min_disk_gb is not None:
        spec["min_disk_gb"] = a.min_disk_gb
    if a.cwd:
        spec["cwd"] = os.path.abspath(os.path.expanduser(a.cwd))
    backend = a.backend or ("launchd" if C.IS_MAC else "systemd")

    warnings = []
    first = command[0]
    if not shutil.which(first, path=f"{os.path.expanduser('~')}/.local/bin:{SYSTEM_PATH}") and not os.path.exists(os.path.expanduser(first)):
        warnings.append(f"'{first}' was not found on the PATH a scheduled job gets ({SYSTEM_PATH} plus ~/.local/bin). Use a full path.")
    if C.IS_MAC:
        touched = [x for x in command + [spec.get("cwd", ""), wrapper_script()] if x and C.under_protected(x)]
        if touched:
            warnings.append("macOS privacy: " + ", ".join(sorted(set(touched))[:3]) + " is inside Desktop/Documents/Downloads.\n"
                            "  launchd jobs cannot read those folders unless the interpreter has Full Disk Access.\n"
                            "  Keep the job's scripts and data elsewhere (for example ~/.local-first/data), and run ./install.sh so the\n"
                            "  wrapper lives in ~/.local-first/lib. See README, 'macOS privacy'.")

    if backend == "launchd":
        body = build_plist(spec, cfg)
        target = plist_path(cfg, a.name)
    elif backend == "systemd":
        service, timer = build_systemd(spec, cfg)
        target = os.path.join(systemd_dir(), label_for(cfg, a.name) + ".timer")
    elif backend == "cron":
        print("Add this line with `crontab -e`:\n" + cron_line(spec, cfg))
        target = None
    else:
        raise SystemExit("localjob: --backend must be launchd, systemd or cron")

    for w in warnings:
        print("warning: " + w, file=sys.stderr)
    if a.dry_run:
        print(f"[dry run] would write {paths(a.name)['spec']}")
        if backend == "launchd":
            print(f"[dry run] would write {target} and load it with launchctl bootstrap\n")
            print(body.decode())
        elif backend == "systemd":
            print(f"[dry run] would write {target} and the matching .service\n\n{service}\n{timer}")
        return 0

    p = paths(a.name)
    C.ensure(os.path.dirname(p["spec"]))
    C.ensure(os.path.dirname(p["log"]))
    if os.path.exists(p["spec"]):                     # re-adding replaces: unload and keep the old copy in the Trash
        _unload(cfg, a.name, backend)
    C.write_json(p["spec"], spec)
    if backend == "launchd":
        C.ensure(os.path.dirname(target))
        if os.path.exists(target):
            C.trash(target)
        open(target, "wb").write(body)
        code, out = launchctl("bootstrap", f"gui/{os.getuid()}", target)
        if code != 0:
            print(f"warning: launchctl bootstrap said: {out}", file=sys.stderr)
    elif backend == "systemd":
        C.ensure(systemd_dir())
        open(os.path.join(systemd_dir(), label_for(cfg, a.name) + ".service"), "w").write(service)
        open(target, "w").write(timer)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", label_for(cfg, a.name) + ".timer"], capture_output=True)
    print(f"Added '{a.name}': {human_schedule(sched)} -> {shlex.join(command)}")
    print(f"Log: {p['log']}   Test it now: localjob run {a.name}")
    return 0


def _unload(cfg, name, backend=None):
    backend = backend or ("launchd" if C.IS_MAC else "systemd")
    if backend == "launchd":
        launchctl("bootout", f"gui/{os.getuid()}/{label_for(cfg, name)}")
    elif backend == "systemd":
        subprocess.run(["systemctl", "--user", "disable", "--now", label_for(cfg, name) + ".timer"], capture_output=True)


def cmd_list(a):
    cfg = C.load_config()
    d = os.path.join(C.lf_home(), "jobs")
    names = sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json") and not f.endswith(".state.json")) if os.path.isdir(d) else []
    if not names:
        print("No jobs yet. Add one:  localjob add disk-check --every 1h -- df -h /")
        return 0
    print(f"{'NAME':<22}{'SCHEDULE':<18}{'LOADED':<8}{'LAST RUN':<20}{'STATUS':<9}COMMAND")
    for n in names:
        spec, st = C.read_json(paths(n)["spec"], {}), C.read_json(paths(n)["state"], {})
        loaded = is_loaded(cfg, n)
        last = datetime.fromtimestamp(st["last_run"]).strftime("%Y-%m-%d %H:%M") if st.get("last_run") else "never"
        print(f"{n:<22}{human_schedule(spec['schedule_parsed']):<18}{'?' if loaded is None else ('yes' if loaded else 'NO'):<8}"
              f"{last:<20}{st.get('last_status', '-'):<9}{shlex.join(spec['argv'])[:60]}")
    return 0


def cmd_run(a):
    r = run_job(a.name, force=a.force, echo=True)
    print(f"-> {r['status']}" + (f" ({r['reason']})" if r.get("reason") else "") + (f", exit {r['rc']}" if r.get("rc") is not None else ""))
    return 0 if r["status"] in ("ok", "skipped", "busy") else (r["rc"] or 1)


def cmd_logs(a):
    p = paths(a.name)["log"]
    if not os.path.exists(p):
        print(f"No log yet for '{a.name}'. It is written when the job first runs ({p}).")
        return 0
    print("\n".join(tail_text(p, 400000).splitlines()[-a.n:]))
    return 0


def cmd_remove(a):
    cfg = C.load_config()
    p = paths(a.name)
    if not os.path.exists(p["spec"]):
        raise SystemExit(f"localjob: no job called '{a.name}'")
    _unload(cfg, a.name)
    moved = []
    for f in (plist_path(cfg, a.name), os.path.join(systemd_dir(), label_for(cfg, a.name) + ".timer"),
              os.path.join(systemd_dir(), label_for(cfg, a.name) + ".service"), p["spec"], p["state"]):
        if os.path.exists(f):
            moved.append(C.trash(f))
    print(f"Removed '{a.name}'. Moved to the Trash: {', '.join(os.path.basename(m) for m in moved)}. The log was kept: {p['log']}")
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="localjob", description="Schedule a plain command. No model, no tokens.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("add", help="add a scheduled job:  localjob add NAME --every 2h -- COMMAND ...")
    s.add_argument("name")
    s.add_argument("--every", required=True, help="30s | 15m | 2h | 1d | daily@06:47")
    s.add_argument("--min-avail-gb", type=float, help="skip the run if less memory than this is available")
    s.add_argument("--min-disk-gb", type=float, help="skip the run if less disk than this is free")
    s.add_argument("--heavy", action="store_true", help="queue behind memguard's heavyrun if it is on PATH")
    s.add_argument("--heavy-light", action="store_true", help="like --heavy, in heavyrun's light lane")
    s.add_argument("--inbox", choices=["auto", "always", "fail", "never"], default="auto",
                   help="when to append a result paragraph to INBOX.md (auto: always, except fail-only for jobs faster than hourly)")
    s.add_argument("--timeout", type=int, default=3600, help="kill the command after this many seconds (default 3600)")
    s.add_argument("--run-at-load", action="store_true", help="also run once when the job is loaded / at login")
    s.add_argument("--cwd", help="working directory (default ~/.local-first)")
    s.add_argument("--python", help="interpreter launchd starts (the one that needs Full Disk Access)")
    s.add_argument("--backend", choices=["launchd", "systemd", "cron"], help="default: launchd on macOS, systemd on Linux")
    s.add_argument("--dry-run", action="store_true", help="show what would be written; change nothing")
    sub.add_parser("list", help="show jobs")
    s = sub.add_parser("run", help="run a job now, in the foreground")
    s.add_argument("name")
    s.add_argument("--force", action="store_true", help="ignore the RAM/disk gate")
    s = sub.add_parser("logs", help="show the end of a job's log")
    s.add_argument("name")
    s.add_argument("-n", type=int, default=40)
    s = sub.add_parser("remove", help="unload a job; its files go to the Trash")
    s.add_argument("name")
    s = sub.add_parser("_wrap")      # what launchd runs
    s.add_argument("name")
    return ap


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command = []
    if "--" in argv:
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1:]
    a = build_parser().parse_args(argv)
    if a.cmd == "add":
        return cmd_add(a, command)
    if a.cmd == "_wrap":
        r = run_job(a.name)
        return 0 if r["status"] != "failed" else (r["rc"] or 1)
    return {"list": cmd_list, "run": cmd_run, "logs": cmd_logs, "remove": cmd_remove}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
