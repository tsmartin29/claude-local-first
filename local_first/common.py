"""Shared helpers: paths, config, resource gate, notifications, Trash, secret redaction, macOS privacy (TCC) hint.

Standard library only. Everything lives under $LOCAL_FIRST_HOME (default ~/.local-first).
"""
import json
import os
import platform
import re
import shutil
import subprocess
import time

GB = 1024 ** 3
IS_MAC = platform.system() == "Darwin"

DEFAULTS = {
    # --- resource gate (a scheduled job is skipped, not failed, when the machine is short of RAM or disk)
    "min_avail_gb": 2.0,
    "min_disk_gb": 5.0,
    # --- reporting
    "inbox": "~/.local-first/INBOX.md",
    "notify": True,
    "log_max_mb": 1.0,
    "log_keep": 3,
    "label_prefix": "com.local-first",
    # --- ladder
    "tasks_dir": "~/.local-first/tasks",
    "handoff_dir": "~/.local-first/handoffs",
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_model": "",                      # empty = first model Ollama reports
    "tier2_command": ["claude-cheap", "haiku", "-p", "{prompt}"],
    "tier3_command": ["claude", "-p", "{prompt}"],
    "max_input_chars": 60000,
    "redact_for_api": True,
    # --- usage meter + suggestions
    "usage_file": "~/.local-first/usage.json",
    "usage_source": "transcripts",           # "transcripts" = estimate from local logs, "file" = you write usage.json yourself
    "projects_dir": "~/.claude/projects",
    "five_hour_budget_tokens": 3000000,
    "weekly_budget_tokens": 40000000,
    "count_cache_reads": False,
    "bands": {"amber_five_hour": 80, "amber_weekly": 85, "amber_model_weekly": 85, "amber_extra": 90, "red": 100},
    "auto_move": False,                      # NOTHING moves tiers on its own unless you opt in here or accept a specific move
    "max_script_suggestions": 3,
    "scan_every_hours": 24,
}
PATH_KEYS = ("inbox", "tasks_dir", "handoff_dir", "usage_file", "projects_dir")


def lf_home():
    return os.path.expanduser(os.environ.get("LOCAL_FIRST_HOME", "~/.local-first"))


def ensure(path):
    os.makedirs(path, exist_ok=True)
    return path


def config_path():
    return os.environ.get("LOCAL_FIRST_CONFIG") or os.path.join(lf_home(), "config.json")


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(config_path()) as fh:
            user = json.load(fh)
        bands = dict(cfg["bands"])
        bands.update(user.get("bands") or {})
        cfg.update(user)
        cfg["bands"] = bands
    except (OSError, ValueError):
        pass
    home = lf_home()
    for k in PATH_KEYS:
        v = cfg[k]
        # defaults that point into ~/.local-first follow LOCAL_FIRST_HOME
        if isinstance(v, str) and v.startswith("~/.local-first"):
            v = home + v[len("~/.local-first"):]
        cfg[k] = os.path.expanduser(v)
    return cfg


def read_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    """Atomic write so a reader (the menu-bar app) never sees half a file."""
    ensure(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------- resource gate
def parse_vm_stat(text):
    """Bytes of memory that can be reclaimed quickly, from `vm_stat` output (free + inactive + speculative + purgeable)."""
    m = re.search(r"page size of (\d+) bytes", text)
    page = int(m.group(1)) if m else 4096
    total = 0
    for label in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"):
        m = re.search(re.escape(label) + r":\s+(\d+)", text)
        if m:
            total += int(m.group(1))
    return total * page


def _run(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=10).stdout


def read_resources(path=None):
    """{'avail': bytes|None, 'disk': bytes|None, 'swap': bytes|None}. None means 'could not read'."""
    res = {"avail": None, "disk": None, "swap": None}
    try:
        if IS_MAC:
            res["avail"] = parse_vm_stat(_run(["vm_stat"]))
            used = _run(["sysctl", "-n", "vm.swapusage"]).split("used = ")[1].split()[0]
            res["swap"] = float(used[:-1]) * (GB if used.endswith("G") else 1024 ** 2)
        elif os.path.exists("/proc/meminfo"):
            for line in open("/proc/meminfo"):
                if line.startswith("MemAvailable:"):
                    res["avail"] = int(line.split()[1]) * 1024
    except Exception:
        pass
    try:
        st = os.statvfs(path or os.path.expanduser("~"))
        res["disk"] = st.f_bavail * st.f_frsize
    except OSError:
        pass
    return res


def gate_check(min_avail_gb=0, min_disk_gb=0, max_swap_gb=None, res=None):
    """Return (ok, reason). A reading that cannot be taken never blocks a job (the gate fails open)."""
    res = res if res is not None else read_resources()
    if min_avail_gb and res.get("avail") is not None and res["avail"] < min_avail_gb * GB:
        return False, f"only {res['avail'] / GB:.1f} GB memory available (need {min_avail_gb:g})"
    if max_swap_gb is not None and res.get("swap") is not None and res["swap"] > max_swap_gb * GB:
        return False, f"swap at {res['swap'] / GB:.1f} GB (limit {max_swap_gb:g})"
    if min_disk_gb and res.get("disk") is not None and res["disk"] < min_disk_gb * GB:
        return False, f"disk has {res['disk'] / GB:.1f} GB free (need {min_disk_gb:g})"
    return True, ""


# ---------------------------------------------------------------- notifications / Trash
def notify(title, msg, enabled=True):
    if not enabled or os.environ.get("LOCAL_FIRST_NO_NOTIFY") or os.environ.get("PYTEST"):
        return False
    msg = str(msg).replace("\\", " ").replace('"', "'")[:200]
    title = str(title).replace("\\", " ").replace('"', "'")
    try:
        if IS_MAC:
            subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "{title}"'], capture_output=True, timeout=10)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", title, msg], capture_output=True, timeout=10)
        else:
            return False
        return True
    except Exception:
        return False


def trash_dir():
    if IS_MAC:
        return os.path.join(os.path.expanduser("~"), ".Trash")
    return os.path.join(os.path.expanduser("~"), ".local", "share", "Trash", "files")


def trash(path):
    """Move a file or folder to the Trash (never deletes). Returns the new path."""
    ensure(trash_dir())
    dest = os.path.join(trash_dir(), os.path.basename(path.rstrip("/")))
    if os.path.exists(dest):
        dest += f".{int(time.time())}"
    shutil.move(path, dest)
    return dest


# ---------------------------------------------------------------- secret redaction
# Prefixes are written in two pieces so this file does not itself look like it contains a key.
_P = [
    r"\bs" r"k-[A-Za-z0-9_-]{16,}",                      # API keys that start with s k dash (several vendors)
    r"\bgh[pousr]" r"_[A-Za-z0-9]{20,}",                  # GitHub tokens
    r"\bgithub" r"_pat_[A-Za-z0-9_]{20,}",
    r"\b[rsp]k" r"_(?:live|test)_[A-Za-z0-9]{10,}",       # payment-provider keys
    r"\bacc" r"t_[A-Za-z0-9]{10,}",                       # payment-provider account ids
    r"\bAKIA[0-9A-Z]{16}\b",                              # AWS access key id
    r"\bxox[abprs]-[A-Za-z0-9-]{10,}",                    # chat-app tokens
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",   # JWTs
    r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}",
    r"://[^/\s:@]+:[^/\s@]+@",                            # user:password@host in URLs
    r"(?i)(?:[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*)=\S+",
    r"(?i)--?(?:password|passwd|token|api[-_]?key|secret)[= ]\S+",
    r"(?i)\bauthorization:\s*\S+(?:\s+\S+)?",
    r"\b[A-Fa-f0-9]{40,}\b",                              # long hex strings
    r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{40,}\b",   # long mixed letter+digit strings (path-free)
]
_SECRET_RE = re.compile("|".join(f"(?:{p})" for p in _P))


def redact(text):
    """Replace anything that looks like a credential with <redacted>. Heuristic: when in doubt it redacts."""
    return _SECRET_RE.sub("<redacted>", text or "")


# ---------------------------------------------------------------- macOS privacy (TCC)
PROTECTED = ("Desktop", "Documents", "Downloads")


def under_protected(path):
    """True if path is inside ~/Desktop, ~/Documents or ~/Downloads (folders launchd jobs cannot read by default)."""
    p = os.path.abspath(os.path.expanduser(path))
    home = os.path.expanduser("~")
    return any(p == os.path.join(home, d) or p.startswith(os.path.join(home, d) + os.sep) for d in PROTECTED)


def looks_like_tcc(text):
    return "Operation not permitted" in (text or "")


def tcc_fix(interpreter, label=None):
    uid = os.getuid()
    kick = f"launchctl kickstart -k gui/{uid}/{label}" if label else "re-run the job"
    return (
        "macOS blocked this job (\"Operation not permitted\"). launchd jobs cannot read ~/Desktop, ~/Documents,\n"
        "~/Downloads, iCloud Drive or removable drives unless the program launchd starts has Full Disk Access.\n"
        "Fix (pick one):\n"
        "  1. Best: keep the job's scripts and data OUTSIDE those folders (for example ~/.local-first/data or ~/bin).\n"
        "  2. Or give the interpreter Full Disk Access: System Settings > Privacy & Security > Full Disk Access > '+',\n"
        f"     press Cmd+Shift+G, paste  {interpreter}  , choose Open, switch it on. Then: {kick}\n"
        "     (If macOS will not let you pick /usr/bin/python3, install Python from python.org or Homebrew and\n"
        "     re-add the job with --python /path/to/that/python3.)"
    )
