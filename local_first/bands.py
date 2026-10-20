"""Usage bands (GREEN / AMBER / RED) and band-crossing detection.

AMBER if any of: 5-hour >= amber_five_hour, weekly >= amber_weekly, one model's weekly >= amber_model_weekly,
extra usage >= amber_extra % of its cap. RED if any of those windows reaches `red` (default 100). Otherwise GREEN.
The numbers come from the usage file (see meter/): exact if you type them in, estimated ("~") if they come from transcripts.
"""
ORDER = {"GREEN": 0, "AMBER": 1, "RED": 2}


def extra_percent(u):
    lim, spent = u.get("extra_limit") or 0, u.get("extra_spent") or 0
    return 100.0 * spent / lim if lim else 0.0


def compute_band(usage, bands):
    """Return (band, [reasons]). `usage` is the dict from usage.json; None/empty means no data, which is GREEN."""
    if not usage:
        return "GREEN", []
    five, wk, mw = usage.get("five_hour") or 0, usage.get("weekly") or 0, usage.get("weekly_model") or 0
    ex = extra_percent(usage)
    red = bands["red"]
    reds, ambers = [], []
    for label, v, amber in (("5-hour", five, bands["amber_five_hour"]), ("weekly", wk, bands["amber_weekly"]),
                            ("one model's weekly", mw, bands["amber_model_weekly"]), ("extra usage", ex, bands["amber_extra"])):
        if v >= red:
            reds.append(f"{label} {v:.0f}% >= {red:g}")
        elif v >= amber:
            ambers.append(f"{label} {v:.0f}% >= {amber:g}")
    if reds:
        return "RED", reds + ambers
    if ambers:
        return "AMBER", ambers
    return "GREEN", []


def advance(state, band, now):
    """Fold a new reading into the saved band state.

    state: {'band','since','crossing'} (or None). A *crossing* is any move UP (green->amber, green->red, amber->red);
    `crossing` counts them. Falling back (red->amber, amber->green) never counts. Returns (new_state, event) with
    event 'up', 'down' or None.
    """
    state = dict(state or {"band": "GREEN", "since": now, "crossing": 0})
    prev = state.get("band", "GREEN")
    event = None
    if ORDER[band] > ORDER[prev]:
        event = "up"
        state["crossing"] = state.get("crossing", 0) + 1
    elif ORDER[band] < ORDER[prev]:
        event = "down"
    if band != prev:
        state["since"] = now
    state["band"] = band
    return state, event
