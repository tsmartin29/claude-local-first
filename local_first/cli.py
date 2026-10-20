#!/usr/bin/env python3
"""localfirst: see and answer the suggestions made when your Claude usage gets high.

  localfirst suggestions            list what is being suggested right now
  localfirst accept <id>            yes, move that one
  localfirst decline <id>           not now (comes back the next time usage rises into a higher band)
  localfirst decline <id> --never   never suggest this again
  localfirst undo <id>              forget a decision you made
  localfirst status                 band, accepted moves, tokens-not-sent estimate
  localfirst refresh                recompute usage band + suggestions (the scheduled job runs this)
Nothing moves tiers unless you accept it, or you set auto_move: true in ~/.local-first/config.json.
"""
import argparse
import datetime
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    from local_first import common as C, suggest, ladder
else:
    from . import common as C, suggest, ladder


def cmd_suggestions(a):
    doc = suggest.refresh(estimate=False, usage=None) if a.refresh else (C.read_json(suggest.suggestions_path(), {}) or {})
    band = doc.get("band", "GREEN")
    items = doc.get("suggestions", [])
    print(f"Usage band: {band}" + (f"  ({'; '.join(doc.get('reasons', []))})" if doc.get("reasons") else ""))
    if not items:
        print("No suggestions right now." if band != "GREEN" else "Usage is green, so nothing is being suggested.")
        return 0
    for s in items:
        print(f"  {s['id']:<22}{s['text']}")
    print("\nlocalfirst accept <id>  |  localfirst decline <id>  |  localfirst decline <id> --never")
    return 0


def cmd_accept(a):
    print(suggest.accept(a.id))
    return 0


def cmd_decline(a):
    print(suggest.decline(a.id, never=a.never))
    return 0


def cmd_undo(a):
    print(suggest.undo(a.id))
    return 0


def cmd_status(a):
    s = suggest.status()
    print(f"Band: {s['band']}" + ("  (estimated from local transcripts, marked ~)" if s["estimate"] and s["band"] != "GREEN" else ""))
    for r in s["reasons"]:
        print(f"  - {r}")
    print(f"Band crossings so far: {s['crossing']}")
    print(f"Suggestions waiting: {len(s['pending'])}" + "".join(f"\n  {p['id']}  {p['text']}" for p in s["pending"]))
    print("Accepted moves:" + ("" if s["accepted"] else " none"))
    for k, v in s["accepted"].items():
        what = f"'{v['task']}' -> {ladder.TIER_NAMES[v['to_tier']]}" if v.get("to_tier") is not None and v.get("task") else f"draft script ({v.get('draft', '')})"
        print(f"  {k}: {what}{' (auto)' if v.get('auto') else ''}  accepted {datetime.datetime.fromtimestamp(v['at']):%Y-%m-%d}")
    print(f"Declined for now: {len(s['declined'])}   Never suggest: {len(s['never'])}")
    print(f"auto_move: {'ON (you opted in)' if s['auto_move'] else 'off (nothing moves without your OK)'}")
    st = s["stats"]
    print(f"Ladder runs: {st['runs']}.  Input not sent to an API: about {ladder.fmt_n(st['est_tokens_not_sent'])} tokens "
          "(ESTIMATE: characters / 4, counted only for runs that stayed on tier 0 or 1).")
    return 0


def cmd_refresh(a):
    doc = suggest.refresh(estimate=a.estimate)
    print(f"{doc['band']}: {len(doc['suggestions'])} suggestion(s)")
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="localfirst", description="Suggestions for moving work to cheaper tiers. You decide.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("suggestions")
    s.add_argument("--refresh", action="store_true", help="recompute first")
    for name in ("accept", "undo"):
        sub.add_parser(name).add_argument("id")
    d = sub.add_parser("decline")
    d.add_argument("id")
    d.add_argument("--never", action="store_true", help="never suggest this again")
    sub.add_parser("status")
    r = sub.add_parser("refresh")
    r.add_argument("--estimate", action="store_true", help="re-estimate usage from local transcripts first")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        return {"suggestions": cmd_suggestions, "accept": cmd_accept, "decline": cmd_decline, "undo": cmd_undo,
                "status": cmd_status, "refresh": cmd_refresh}[a.cmd](a)
    except KeyError as ex:
        print(f"localfirst: {ex.args[0]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
