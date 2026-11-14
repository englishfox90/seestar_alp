"""The sky over a night for Tonight: sun and moon altitude, and the roof.

Runs from a little before sunset to a little after sunrise, so the sun is seen setting
and rising. Altitudes are drawn into an SVG viewBox (VIEW_W x VIEW_H) that the page
stretches to its width; times along the bottom and the roof band are percentages of
the same span, so HTML and SVG line up.

The roof band is what's known before now (the roof record: open, closed, or no
record) and the forecast after it (roof.forecast's hours). For a person to read:
nothing here decides anything.
"""

import math
from datetime import datetime, timezone

import ephem

MARGIN_S = 75 * 60  # chart runs from sunset - MARGIN_S to sunrise + MARGIN_S
STEP_S = 10 * 60
VIEW_W, VIEW_H = 1000, 120
ALT_TOP = 90.0  # the highest the plot reaches, in degrees
# The horizon sits a quarter up from the bottom at any scale: room for the rise and set
# times. Below it runs down to astronomical twilight, where the dark band starts; curves
# go on past the floor and the SVG clip cuts them, so nothing runs along the bottom.
HORIZON_AT = 0.75
ALT_BOTTOM = -18.0
ALT_HEADROOM, ALT_STEP, ALT_MIN_TOP = 10.0, 15.0, 30.0
# The roof's expected window, as in roof.forecast.
ROOF_OPEN_OFFSET_S = -30 * 60
ROOF_CLOSE_OFFSET_S = 30 * 60


def _ephem_date(when: float):
    return ephem.Date(datetime.fromtimestamp(when, timezone.utc).replace(tzinfo=None))


def altitudes(lat, lon, times) -> dict:
    """Sun and moon altitude in degrees at each time, and the moon's lit percent."""
    observer = ephem.Observer()
    observer.lat, observer.lon = str(lat), str(lon)
    observer.pressure = 0
    sun, moon = ephem.Sun(), ephem.Moon()
    out = {"sun": [], "moon": [], "moon_lit": []}
    for when in times:
        observer.date = _ephem_date(when)
        sun.compute(observer)
        moon.compute(observer)
        out["sun"].append(math.degrees(sun.alt))
        out["moon"].append(math.degrees(moon.alt))
        out["moon_lit"].append(moon.phase)
    return out


def y_of(alt: float, top: float = ALT_TOP) -> float:
    """Above the horizon scaled to ``top``, below it to ALT_BOTTOM, each side on its own."""
    if alt >= 0:
        frac = HORIZON_AT * (1 - min(alt, top) / top)
    else:
        frac = HORIZON_AT + (1 - HORIZON_AT) * max(alt, -90.0) / ALT_BOTTOM
    return round(frac * VIEW_H, 2)


def plot_top(*series) -> float:
    """The plot's top altitude: the night's highest with some headroom, in steps,
    so a low winter moon isn't drawn in a strip along the horizon."""
    highest = max((max(s) for s in series if s), default=0.0)
    return min(ALT_TOP, max(ALT_MIN_TOP, math.ceil((highest + ALT_HEADROOM) / ALT_STEP) * ALT_STEP))


def phase_name(lit: float, waxing: bool) -> str:
    if lit < 3:
        return "New moon"
    if lit > 97:
        return "Full moon"
    if 45 <= lit <= 55:
        return "First quarter" if waxing else "Last quarter"
    shape = "crescent" if lit < 50 else "gibbous"
    return f"{'Waxing' if waxing else 'Waning'} {shape}"


def moon_shadow(lit: float, waxing: bool) -> float:
    """Offset of the shadow disc, in radii: 0 hides the moon, ±2 clears it. Waxing is
    lit on the right as seen from the north, so its shadow sits to the left."""
    return round(2 * lit / 100, 2) * (-1 if waxing else 1)


def crossings(times, alts) -> list[tuple[float, str]]:
    """When a body crosses the horizon between samples: (time, "rise" | "set")."""
    found = []
    for (t0, a0), (t1, a1) in zip(zip(times, alts), zip(times[1:], alts[1:])):
        if (a0 < 0) != (a1 < 0):
            found.append((t0 + (0 - a0) / (a1 - a0) * (t1 - t0), "rise" if a1 >= 0 else "set"))
    return found


def _clip(spans, lo, hi):
    out = []
    for start, end, state in spans:
        start, end = max(start, lo), min(end, hi)
        if end > start:
            out.append((start, end, state))
    return out


def roof_spans(roof_open, roof_close, now, record=None, hours=None) -> list[tuple[float, float, str]]:
    """The roof's window split into what happened and what's forecast.

    States: "open", "closed", "unknown" before now; "fc-open", "fc-maybe", "fc-closed" after.
    ``record`` is NightHistory.roof_record (closed periods may run on with end None).
    """
    spans = []
    past_end = min(now, roof_close) if now is not None else roof_open
    if record is not None and past_end > roof_open:
        marks = []
        for period in record.get("closed") or ():
            end = period["end"] if period["end"] is not None else past_end
            marks.append((period["start"], end, "closed"))
        for period in record.get("unknown") or ():
            marks.append((period["start"], period["end"], "unknown"))
        marks = sorted(_clip(marks, roof_open, past_end))
        at = roof_open
        for start, end, state in marks:
            if start > at:
                spans.append((at, start, "open"))
            if end > at:
                spans.append((max(start, at), end, state))
                at = end
        if at < past_end:
            spans.append((at, past_end, "open"))
    future_start = max(now if now is not None else roof_open, roof_open)
    for h in hours or ():
        start, end = max(h["start"], future_start), min(h["end"], roof_close)
        if end > start:
            spans.append((start, end, "fc-" + h["state"]))
    merged = []
    for span in spans:
        if merged and merged[-1][2] == span[2] and merged[-1][1] >= span[0]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], span[1]), span[2])
        else:
            merged.append(span)
    return merged


def _buildings(street, roof_safe) -> list:
    """Three buildings for the horizon, all showing our roof's state: Starfront's feed
    for our building, else the roof monitor. Without the feed, ours alone."""
    fallback = None if roof_safe is None else bool(roof_safe)
    if not street:
        return [{"label": "", "open": fallback, "ours": True}]
    ours = next((b for b in street if b["ours"]), None)
    state = ours["open"] if ours is not None and ours["open"] is not None else fallback
    return [{"label": str(b["number"]), "open": state, "ours": b["ours"]} for b in street]


_ROOF_LABELS = {
    "open": "Open", "closed": "Closed", "unknown": "No record",
    "fc-open": "Likely open", "fc-maybe": "Might open", "fc-closed": "Likely closed",
}  # fmt: skip


def _roof_lane(spans, pct) -> list:
    """The roof lane's spans with labels where they fit: tier 1 shows on phones too,
    tier 2 only on wider screens. The first label names the lane."""
    out, named = [], False
    for s, e, state in spans:
        start, end = pct(s), pct(e)
        width = end - start
        tier = 1 if width >= 28 else 2 if width >= 14 else None
        label = None
        if tier:
            label = ("Roof: " if not named else "") + _ROOF_LABELS[state]
            named = True
        out.append({"start": start, "end": end, "state": state, "label": label, "tier": tier})
    return out


_ROOF_WORDS = {
    "open": "Roof open",
    "closed": "Roof closed",
    "unknown": "No roof record",
    "fc-open": "Roof likely open",
    "fc-maybe": "Roof might open",
    "fc-closed": "Roof likely closed",
}


def sky_chart(window, lat, lon, now=None, clock=str, record=None, outlook=None, roof_safe=None, buildings=None) -> dict:
    """Everything the page draws. ``window`` is the plan's night (sunset, sunrise,
    dark_start, dark_end); ``outlook`` is the night's entry from roof.forecast;
    ``buildings`` is site_status.street: ours between its neighbours, each roof as
    Starfront's feed reports it. Ours falls back to ``roof_safe`` when the feed has
    no fresh answer."""
    window = window or {}
    sunset, sunrise = window.get("sunset"), window.get("sunrise")
    if sunset is None or sunrise is None or sunrise <= sunset or not (lat or lon):
        return {"available": False}
    start, end = sunset - MARGIN_S, sunrise + MARGIN_S
    span = end - start

    def pct(when):
        return round(max(0.0, min(100.0, (when - start) * 100 / span)), 2)

    times = [start + k * STEP_S for k in range(int(span // STEP_S) + 1)]
    if times[-1] < end:
        times.append(end)
    alt = altitudes(lat, lon, times)
    top = plot_top(alt["sun"], alt["moon"])

    def path(values):
        return "M" + " L".join(f"{round((t - start) * VIEW_W / span, 1)},{y_of(a, top)}" for t, a in zip(times, values))

    words = [f"Sun sets {clock(sunset)} and rises {clock(sunrise)}."]
    def event(body, kind, when):
        at = pct(when)
        word = {"sun": "Sun", "moon": "Moon"}[body] + kind
        return {
            "body": body, "kind": kind, "at": at, "word": word, "time": clock(when), "text": f"{word} {clock(when)}",
            "edge": "start" if at < 10 else "end" if at > 90 else "",
        }  # fmt: skip

    horizon = [event("sun", "set", sunset), event("sun", "rise", sunrise)]
    moon_events = crossings(times, alt["moon"])
    for when, kind in moon_events:
        horizon.append(event("moon", kind, when))
        words.append(f"Moon{kind} {clock(when)}.")
    lit = round(max(alt["moon_lit"]))
    mid = len(times) // 2
    waxing = alt["moon_lit"][-1] >= alt["moon_lit"][0]
    # The moon's phase, drawn where its arc is highest over the night.
    phase = None
    peak = max(range(len(times)), key=lambda i: alt["moon"][i])
    if alt["moon"][peak] > 0:
        phase = {
            "x": min(96.0, max(4.0, pct(times[peak]))),
            "y": round(y_of(alt["moon"][peak], top) * 100 / VIEW_H, 2),
            "lit": round(alt["moon_lit"][peak]),
            "shadow": moon_shadow(alt["moon_lit"][peak], waxing),
            "name": phase_name(alt["moon_lit"][peak], waxing),
            "edge": "start" if peak < len(times) * 0.12 else "end" if peak > len(times) * 0.88 else "mid",
        }
        words.append(f"{phase['name']}, {phase['lit']}% lit, highest at {clock(times[peak])}.")
    if not moon_events:
        words.append("Moon up all night." if alt["moon"][mid] >= 0 else "Moon down all night.")
    if phase is None:
        words.append(f"{phase_name(lit, waxing)}, {lit}% lit.")

    bodies = []
    if now is not None and start <= now <= end:
        here = altitudes(lat, lon, [now, now + 3600])
        for name in ("sun", "moon"):
            a = here[name][0]
            body = {
                "name": name,
                "x": pct(now),
                "y": round(y_of(a, top) * 100 / VIEW_H, 2),
                "up": a >= 0,
                # Below the plot's floor: drawn only while the intro rides down to it.
                "gone": a < ALT_BOTTOM,
                "alt": round(a),
            }
            if name == "moon":
                body["lit"] = round(here["moon_lit"][0])
                body["waxing"] = here["moon_lit"][1] >= here["moon_lit"][0]
                body["shadow"] = moon_shadow(body["lit"], body["waxing"])
            bodies.append(body)
        words.append(f"Now the sun is at {bodies[0]['alt']}° and the moon at {bodies[1]['alt']}°.")

    roof_open, roof_close = sunset + ROOF_OPEN_OFFSET_S, sunrise + ROOF_CLOSE_OFFSET_S
    roof = roof_spans(roof_open, roof_close, now, record, (outlook or {}).get("hours"))
    for s, e, state in roof:
        words.append(f"{_ROOF_WORDS[state]} {clock(s)} to {clock(e)}.")
    ours = next(b for b in _buildings(buildings, roof_safe) if b["ours"])
    state = "open" if ours["open"] else "closed" if ours["open"] is False else "unknown"
    words.append(f"{'Building ' + ours['label'] if ours['label'] else 'The'} roof is {state} now.")

    ticks, grid = [], []
    first_hour = int(start // 3600 + 1) * 3600
    for t in range(first_hour, int(end), 3600):
        grid.append(round(pct(t) * VIEW_W / 100, 1))
        label = clock(t)[:2]
        if label.isdigit() and int(label) % 2 == 0:
            ticks.append({"at": pct(t), "text": label})

    dark = None
    if window.get("dark_start") is not None and window.get("dark_end") is not None:
        dark = {"x": round(pct(window["dark_start"]) * VIEW_W / 100, 1), "w": round((pct(window["dark_end"]) - pct(window["dark_start"])) * VIEW_W / 100, 1)}

    return {
        "available": True,
        "view": {"w": VIEW_W, "h": VIEW_H, "horizon": y_of(0, top)},
        "horizon_pct": round(y_of(0, top) * 100 / VIEW_H, 2),
        "horizon_events": horizon,
        "phase": phase,
        "grid": grid,
        # The observatory sits on the horizon in the middle of the night.
        "middle": pct((sunset + sunrise) / 2),
        "sun_path": path(alt["sun"]),
        "moon_path": path(alt["moon"]),
        "dark": dark,
        "sunset": pct(sunset),
        "sunrise": pct(sunrise),
        "bodies": bodies,
        "now": pct(now) if now is not None and start <= now <= end else None,
        "roof": _roof_lane(roof, pct),
        # Ours as the building feed reports it, else the roof monitor.
        "roof_now": next(b["open"] for b in _buildings(buildings, roof_safe) if b["ours"]),
        "buildings": _buildings(buildings, roof_safe),
        "forecast": (outlook or {}).get("headline"),
        "moon_lit": lit,
        "moon_name": phase_name(lit, waxing),
        "ticks": ticks,
        "label": " ".join(words),
    }
