"""Your decisions about suggested moves, stored in ~/.local-first/decisions.json.

Each item: {"status": "accepted" | "declined" | "never", "crossing": N, "at": ts, ...}
  accepted  - you said yes to this specific move (task moves also store "task" and "to_tier")
  declined  - "not now": hidden until the NEXT band crossing (usage rising into a higher band), then offered again
  never     - "never suggest this again": respected forever, until you run `localfirst undo <id>`
"""
import os
import time

from . import common as C


def path():
    return os.path.join(C.lf_home(), "decisions.json")


def load():
    d = C.read_json(path(), {})
    d.setdefault("version", 1)
    d.setdefault("items", {})
    return d


def save(d):
    C.write_json(path(), d)


def record(item_id, status, crossing=0, now=None, **extra):
    assert status in ("accepted", "declined", "never")
    d = load()
    d["items"][item_id] = dict(extra, status=status, crossing=crossing, at=now or time.time())
    save(d)
    return d["items"][item_id]


def forget(item_id):
    d = load()
    gone = d["items"].pop(item_id, None)
    save(d)
    return gone


def suggestable(item, crossing):
    """Should this id be (re)offered at the current crossing count?"""
    if not item:
        return True
    st = item.get("status")
    if st in ("accepted", "never"):
        return False
    if st == "declined":
        return crossing > item.get("crossing", 0)
    return True


def accepted_tier(task_name, d=None):
    item = (d or load())["items"].get("t-" + task_name)
    if item and item.get("status") == "accepted":
        return item.get("to_tier")
    return None
