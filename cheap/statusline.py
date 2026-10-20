#!/usr/bin/env python3
"""Cheap-mode status line for Claude Code: model, folder, OpenRouter credit left, inbox count.

Claude Code pipes session JSON to stdin and shows the first line printed. This must be fast, so the credit lookup
(GET https://openrouter.ai/api/v1/credits) runs in the background at most every 2 minutes and the line shows the cached value.
The only network call is that one request to OpenRouter, with your own key; nothing else leaves the machine.

Settings (environment variables, all optional):
  CHEAP_INBOX              inbox file to count entries in         (default ~/.local-first/INBOX.md)
  CHEAP_KEYCHAIN_SERVICE   macOS Keychain service holding the key (default local-first-openrouter)
  CHEAP_CACHE_DIR          where the cached credit is kept        (default ~/.cache/local-first)
  CHEAP_LABEL              the badge text                         (default CHEAP MODE)
The key is taken from ANTHROPIC_AUTH_TOKEN when claude-cheap started this session (it is already in the environment),
else OPENROUTER_API_KEY, else the Keychain.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

HOME = os.path.expanduser("~")
INBOX = os.path.expanduser(os.environ.get("CHEAP_INBOX") or os.path.join(os.environ.get("LOCAL_FIRST_HOME", "~/.local-first"), "INBOX.md"))
SERVICE = os.environ.get("CHEAP_KEYCHAIN_SERVICE", "local-first-openrouter")
CACHE = os.path.join(os.path.expanduser(os.environ.get("CHEAP_CACHE_DIR", "~/.cache/local-first")), "openrouter_credit.json")
LABEL = os.environ.get("CHEAP_LABEL", "CHEAP MODE")
A, D, G, Y, R, B, X = "\033[38;5;214m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[0m"


def find_key():
    if "openrouter" in os.environ.get("ANTHROPIC_BASE_URL", "") and os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return os.environ["ANTHROPIC_AUTH_TOKEN"]
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    try:
        return subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", "openrouter", "-w"],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def refresh():
    try:
        key = find_key()
        if not key:
            return
        req = urllib.request.Request("https://openrouter.ai/api/v1/credits", headers={"Authorization": "Bearer " + key})
        d = json.load(urllib.request.urlopen(req, timeout=8))["data"]
        left = float(d["total_credits"]) - float(d["total_usage"])
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        tmp = CACHE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"left": left, "at": time.time()}, fh)
        os.replace(tmp, CACHE)
    except Exception:
        pass


def short_model(name):
    name = name.split("/")[-1]
    return name.replace("glm-", "GLM ").replace("deepseek-", "DeepSeek ").replace("claude-", "Claude ")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--refresh":
        refresh()
        return
    try:
        s = json.load(sys.stdin)
    except Exception:
        s = {}
    model = short_model((s.get("model") or {}).get("display_name") or (s.get("model") or {}).get("id") or "?")
    cwd = ((s.get("workspace") or {}).get("current_dir") or os.getcwd()).replace(HOME, "~")
    if len(cwd) > 38:
        cwd = "…" + cwd[-37:]
    credit, stale = "", True
    try:
        c = json.load(open(CACHE))
        left = c["left"]
        col = R if left < 2 else (Y if left < 5 else G)
        credit = f"{col}${left:.2f} left{X}"
        stale = time.time() - c["at"] > 120
    except Exception:
        pass
    if stale:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--refresh"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    inbox = ""
    try:
        t = open(INBOX).read()
        inbox = f"inbox {t.count(chr(10) + '## ') + (1 if t.startswith('## ') else 0)}"
    except Exception:
        pass
    parts = [f"{A}{B}⚡ {LABEL}{X}", f"{B}{model}{X}", f"{D}{cwd}{X}", credit or f"{D}credit …{X}"] + ([f"{D}{inbox}{X}"] if inbox else [])
    print(f" {D}·{X} ".join(parts))


if __name__ == "__main__":
    main()
