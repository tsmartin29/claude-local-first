#!/usr/bin/env python3
"""Estimate Claude Code usage from YOUR OWN local transcripts and write the usage file the menu-bar meter reads.

Reads ~/.claude/projects/**/*.jsonl (Claude Code's local session logs). Nothing leaves your machine; no network, no
tokens, no cookies, no private Claude-app API. It sums the token counts of assistant replies in the last 5 hours and
7 days and divides by budgets YOU set in ~/.local-first/config.json. Anthropic does not publish plan limits as token
counts, so this is an ESTIMATE (the meter marks it with "~"). Calibrate the budgets once: see the README.

Config keys (all optional, in ~/.local-first/config.json):
  five_hour_budget_tokens, weekly_budget_tokens, count_cache_reads, projects_dir, usage_file, bands
Usage: usage_from_transcripts.py [--print]
"""
import datetime as dt
import glob
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from local_first import bands as B, common as C   # noqa: E402


def parse_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def tokens(usage, cache_reads):
    t = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0)
    if cache_reads:
        t += usage.get("cache_read_input_tokens") or 0
    return t


def collect(cfg, now):
    """[(timestamp, tokens)] for assistant replies in the last 7 days (each API request counted once)."""
    root = os.path.expanduser(cfg["projects_dir"])
    cutoff, seen, out = now - 7 * 86400, set(), []
    for f in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(f) < cutoff:
                continue
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if '"usage"' not in line or '"assistant"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("type") != "assistant":
                        continue
                    u = (d.get("message") or {}).get("usage")
                    if not u:
                        continue
                    key = d.get("requestId") or (d.get("message") or {}).get("id") or d.get("uuid")
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        ts = parse_ts(d["timestamp"])
                    except Exception:
                        continue
                    if ts >= cutoff:
                        out.append((ts, tokens(u, cfg["count_cache_reads"])))
        except OSError:
            continue
    return out


def compute(cfg, now=None):
    now = now or time.time()
    rows = collect(cfg, now)
    five = sum(t for ts, t in rows if ts >= now - 5 * 3600)
    week = sum(t for ts, t in rows)
    p5 = 100 * five / cfg["five_hour_budget_tokens"]
    pw = 100 * week / cfg["weekly_budget_tokens"]
    inside = [ts for ts, _ in rows if ts >= now - 5 * 3600]
    left = int(min(inside) + 5 * 3600 - now) if inside else 0       # the window "resets" 5 h after the oldest reply in it
    data = {"five_hour": round(p5), "five_hour_resets_in": f"{left // 3600}h {left % 3600 // 60}m" if inside else "-",
            "weekly": round(pw), "estimate": True,
            "five_hour_tokens": five, "weekly_tokens": week,        # use these to calibrate the budgets
            "updated": dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    data["zone"] = B.compute_band(data, cfg["bands"])[0]
    if not cfg.get("budgets_calibrated", False):        # default budgets are guesses: show grey, never a false RED
        data["zone"] = "UNCALIBRATED"
    return data


if __name__ == "__main__":
    cfg = C.load_config()
    data = compute(cfg)
    if "--print" in sys.argv:
        print(json.dumps(data, indent=2))
    else:
        C.write_json(cfg["usage_file"], data)
