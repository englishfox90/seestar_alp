"""Will Starfront's roof open tonight? A forecast for a person to read.

Two models, each read hour by hour over the roof's night (open from 30 min before
sunset to 30 min after sunrise, weather permitting):

- ClearDarkSky: the site's Clear Sky Chart. It has no API, so the GIF is decoded by
  the colour of each hourly cell (cloud, humidity), the same way the hermes
  ``cleardarksky_tools`` does.
- Open-Meteo: hourly cloud, humidity, dew point and visibility.

Only cloud and dew close Starfront's roof: it opens when there's no cloud cover, and
stays shut for cloud or high dew (humidity, dew point at the temperature, fog). Rain
and wind aren't rated, since a night that rains is a cloudy one anyway.

Each hour reads open, maybe or closed. The thresholds come from this rig's own roof
record (roof_night_history.json against Open-Meteo's history, Nov 2026): cloud closes
the roof; humidity up to ~93% with the dew point 1-2 °C away did not, but fog and
low cloud forming at that humidity did. They are estimates, so they're kept here in
one place to tune.

Nothing here decides anything: the automation answers only to the roof monitor.
Fetched in the background at most every REFRESH_S, so a page never waits on it, and
a failed fetch keeps the last answer.
"""

import threading
import time
from datetime import datetime, timedelta
from io import BytesIO

import requests

from roof.planner import night_times
from roof.sky_window import current_night

CSC_URL = "https://www.cleardarksky.com/c/StrfrntObsTXcsk.gif"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
REFRESH_S = 30 * 60
RETRY_S = 5 * 60
TIMEOUT_S = 10
NIGHTS = 3
# Open-Meteo's longest forecast, for the Plan page's two weeks.
METEO_DAYS = 16
# The roof's expected window, weather permitting: from 30 min before sunset to 30 min
# after sunrise. (site_status still shows Starfront's published 15 min / 5 min.)
ROOF_OPEN_OFFSET_S = -30 * 60
ROOF_CLOSE_OFFSET_S = 30 * 60

OPEN, MAYBE, CLOSED = 0, 1, 2
STATE_WORDS = {OPEN: "open", MAYBE: "maybe", CLOSED: "closed"}

# --- Thresholds (tune here) -------------------------------------------------------
CLOUD_CLOSED_PCT = 60
CLOUD_MAYBE_PCT = 30
HUMIDITY_CLOSED_PCT = 97  # at this the site is in or near fog
HUMIDITY_MAYBE_PCT = 95  # dew and fog likely
FOG_SPREAD_C = 0.5  # temperature minus dew point
FOG_VISIBILITY_M = 2000

# --- Chance the roof is open, for the Plan page's forecast (tune here) ---------------
OPEN_CHANCE = {OPEN: 0.95, MAYBE: 0.5, CLOSED: 0.05}
# Hourly forecasts are taken at their word this far ahead, then trusted less and less
# until, at TRUST_NONE_S, only the site's typical chance (Settings.typical_open_chance)
# is left. Cloud forecasts lose most of their skill after about a week.
TRUST_FULL_S = 2 * 86400
TRUST_NONE_S = 10 * 86400

# --- Clear Sky Chart layout (1284x408, probed by hermes cleardarksky_tools) --------
_LEFT, _COL_W, _COL_OFF = 134, 14, 6


def _hex(h):
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


_CSC_CLOUD = [
    (_hex(c), v)
    for c, v in [
        ("003E7E", 0), ("135393", 10), ("2666A6", 20), ("4E8ECE", 30), ("62A2E2", 40), ("76B6F6", 50),
        ("99D9D9", 60), ("ADEDED", 70), ("C1C1C1", 80), ("E9E9E9", 90), ("FAFAFA", 100),
    ]
]  # fmt: skip
_CSC_HUMIDITY = [
    (_hex(c), v)
    for c, v in [
        ("08035D", 22), ("0D4D8D", 27), ("3070B0", 32), ("4E8ECE", 37), ("71B1F1", 42), ("80C0C0", 47),
        ("09FEED", 52), ("55FAAD", 57), ("94FE6A", 62), ("EAFB16", 67), ("FEC600", 72), ("FC8602", 77),
        ("FE3401", 82), ("EA0000", 87), ("B70000", 92), ("E10000", 97),
    ]
]  # fmt: skip
# (name, y, table). Row y-midpoints, top to bottom.
_CSC_ROWS = [("cloud", 83, _CSC_CLOUD), ("humidity", 226, _CSC_HUMIDITY)]
_CSC_MIDNIGHT_ROWS = (83, 98, 114, 130)


def _nearest(pixel, table):
    return min(table, key=lambda e: sum((a - b) ** 2 for a, b in zip(pixel[:3], e[0])))[1]


def decode_csc(data: bytes, fetched_at: float, tz) -> list[dict]:
    """Hourly rows from the chart GIF: {"at", "cloud", "humidity"}.

    Times come from the red midnight line. The chart starts at its last model run,
    often the evening before, so the first line can be a midnight already past: it's
    the latest midnight that puts the first column at or before the fetch.
    """
    from PIL import Image

    img = Image.open(BytesIO(data)).convert("RGB")
    width, height = img.size
    px = img.load()
    cols = (width - _LEFT) // _COL_W
    if cols <= 0 or height <= max(y for _, y, _ in _CSC_ROWS):
        raise ValueError(f"unexpected chart size {width}x{height}")

    red_x = None
    for y in _CSC_MIDNIGHT_ROWS:
        for x in range(_LEFT, width):
            r, g, b = px[x, y]
            if r > 150 and g < 60 and b < 60:
                red_x = x if red_x is None else min(red_x, x)
                break
    fetched = datetime.fromtimestamp(fetched_at, tz)
    if red_x is None:
        first = fetched.replace(minute=0, second=0, microsecond=0).timestamp()
    else:
        midnight_col = min((red_x - _LEFT + _COL_W - 1) // _COL_W, cols - 1)
        first = None
        for days in (1, 0, -1):
            day = (fetched + timedelta(days=days)).date()
            midnight = datetime(day.year, day.month, day.day, tzinfo=tz).timestamp()
            first = midnight - midnight_col * 3600
            if first <= fetched_at:
                break

    rows = []
    for col in range(cols):
        x = min(_LEFT + col * _COL_W + _COL_OFF, width - 1)
        row = {"at": first + col * 3600}
        for name, y, table in _CSC_ROWS:
            row[name] = _nearest(px[x, y], table)
        rows.append(row)
    return rows


def decode_open_meteo(reply: dict) -> list[dict]:
    """Hourly rows from Open-Meteo's reply, asked for in unixtime."""
    hourly = reply["hourly"]
    keys = {
        "cloud": "cloud_cover",
        "humidity": "relative_humidity_2m",
        "temp_c": "temperature_2m",
        "dew_point_c": "dew_point_2m",
        "visibility_m": "visibility",
    }
    rows = []
    for i, at in enumerate(hourly["time"]):
        row = {"at": float(at)}
        for name, key in keys.items():
            values = hourly.get(key) or []
            row[name] = values[i] if i < len(values) else None
        rows.append(row)
    return rows


# --- Rating -------------------------------------------------------------------------
# A reason is (state, kind, value, text). Kinds group them for the night's summary.


def _cloud(value):
    if value is None:
        return []
    if value >= CLOUD_CLOSED_PCT:
        return [(CLOSED, "cloud", value, f"cloud {value:.0f}%")]
    if value >= CLOUD_MAYBE_PCT:
        return [(MAYBE, "cloud", value, f"cloud {value:.0f}%")]
    return []


def rate_open_meteo(row) -> list:
    reasons = _cloud(row.get("cloud"))
    humidity, visibility = row.get("humidity"), row.get("visibility_m")
    temp, dew = row.get("temp_c"), row.get("dew_point_c")
    spread = temp - dew if temp is not None and dew is not None else None
    if (visibility is not None and visibility < FOG_VISIBILITY_M) or (spread is not None and spread <= FOG_SPREAD_C):
        reasons.append((CLOSED, "fog", humidity or 100, "fog likely"))
    elif humidity is not None and humidity >= HUMIDITY_CLOSED_PCT:
        reasons.append((CLOSED, "humidity", humidity, f"humidity {humidity:.0f}%"))
    elif humidity is not None and humidity >= HUMIDITY_MAYBE_PCT:
        reasons.append((MAYBE, "humidity", humidity, f"humidity {humidity:.0f}%"))
    return reasons


def rate_csc(row) -> list:
    """The chart's humidity is a coarse bucket, so it only ever says maybe."""
    reasons = _cloud(row.get("cloud"))
    if row.get("humidity", 0) >= HUMIDITY_MAYBE_PCT:
        reasons.append((MAYBE, "humidity", row["humidity"], "humidity 95%+"))
    return reasons


def _state(reasons) -> int:
    return max((r[0] for r in reasons), default=OPEN)


def combine(states) -> int:
    """Two models that agree within a step give the worse; open against closed is maybe."""
    states = [s for s in states if s is not None]
    if not states:
        return None
    if max(states) - min(states) >= 2:
        return MAYBE
    return max(states)


def _by_hour(rows):
    return {int(r["at"] // 3600): r for r in rows or ()}


def rate_hours(start, end, csc_rows, meteo_rows) -> list[dict]:
    """Every hour from start to end that at least one model covers."""
    csc, meteo = _by_hour(csc_rows), _by_hour(meteo_rows)
    hours = []
    hour = int(start // 3600)
    while hour * 3600 < end:
        reasons_by = {}
        if hour in csc:
            reasons_by["ClearDarkSky"] = rate_csc(csc[hour])
        if hour in meteo:
            reasons_by["Open-Meteo"] = rate_open_meteo(meteo[hour])
        if reasons_by:
            state = combine([_state(r) for r in reasons_by.values()])
            hours.append(
                {
                    "start": max(hour * 3600, start),
                    "end": min((hour + 1) * 3600, end),
                    "state": state,
                    "reasons": [r for rs in reasons_by.values() for r in rs if r[0] > OPEN],
                }
            )
        hour += 1
    return hours


def _spans(hours):
    spans = []
    for h in hours:
        if spans and spans[-1]["state"] == h["state"] and spans[-1]["end"] >= h["start"]:
            spans[-1]["end"] = h["end"]
        else:
            spans.append({"start": h["start"], "end": h["end"], "state": h["state"]})
    return spans


_KIND_WORDS = {
    "cloud": lambda v: f"cloud up to {v:.0f}%",
    "humidity": lambda v: f"humidity up to {v:.0f}%, so dew or fog",
    "fog": lambda v: "fog likely",
}


def why(hours) -> str:
    """The causes behind the hours that won't open, worst and most frequent first."""
    seen = {}
    for h in hours:
        if h["state"] == OPEN:
            continue
        for state, kind, value, _ in h["reasons"]:
            count, peak, worst = seen.get(kind, (0, value, state))
            seen[kind] = (count + 1, max(peak, value), max(worst, state))
    if not seen:
        return ""
    order = sorted(seen.items(), key=lambda kv: (-kv[1][2], -kv[1][0]))
    words = [_KIND_WORDS[kind](peak) for kind, (_, peak, _) in order[:3]]
    text = "; ".join(words)
    return text[0].upper() + text[1:] + "."


def verdict(hours, clock) -> dict:
    """The night in one line: open, partly or closed, with when and why."""
    if not hours:
        return {"key": "unknown", "tone": "neutral", "headline": "No forecast for these hours", "detail": ""}
    total = sum(h["end"] - h["start"] for h in hours)
    share = {s: sum(h["end"] - h["start"] for h in hours if h["state"] == s) / total for s in (OPEN, MAYBE, CLOSED)}
    spans = _spans(hours)
    open_spans = [s for s in spans if s["state"] == OPEN and s["end"] - s["start"] >= 3600]
    maybe_spans = [s for s in spans if s["state"] == MAYBE and s["end"] - s["start"] >= 3600]
    when = lambda ss: ", ".join(f"{clock(s['start'])}–{clock(s['end'])}" for s in ss[:2])  # noqa: E731
    if share[OPEN] >= 0.9:
        key, tone, headline = "open", "ok", "Roof likely open"
    elif share[CLOSED] >= 0.8 or (not open_spans and not maybe_spans):
        key, tone, headline = "closed", "danger", "Roof likely closed"
    elif open_spans:
        key, tone, headline = "partly", "attention", f"Roof likely open {when(open_spans)}"
    else:
        key, tone, headline = "partly", "attention", f"Roof might open {when(maybe_spans)}"
    return {"key": key, "tone": tone, "headline": headline, "detail": why(hours)}


def dew_note(meteo_rows, start, end, clock, margin_c=2.0) -> str:
    """When the optics will want heat even under a clear sky."""
    for row in meteo_rows or ():
        if start <= row["at"] < end and row.get("temp_c") is not None and row.get("dew_point_c") is not None:
            if row["temp_c"] - row["dew_point_c"] <= margin_c:
                return f"Dew point within {margin_c:.0f} °C of the temperature from {clock(max(row['at'], start))}."
    return ""


# --- Fetching -----------------------------------------------------------------------


def fetch_csc(get=requests.get) -> bytes:
    reply = get(CSC_URL, timeout=TIMEOUT_S, headers={"User-Agent": "seestar_alp roof forecast"})
    reply.raise_for_status()
    return reply.content


def fetch_open_meteo(lat, lon, get=requests.get) -> dict:
    reply = get(
        OPEN_METEO_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": "cloud_cover,relative_humidity_2m,temperature_2m,dew_point_2m,visibility",
            "timeformat": "unixtime",
            "forecast_days": METEO_DAYS,
        },
        timeout=TIMEOUT_S,
    )
    reply.raise_for_status()
    return reply.json()


class _Source:
    """One model's hourly rows, refreshed off the request thread."""

    def __init__(self, load, clock=time.time):
        self._load = load
        self._clock = clock
        self._lock = threading.Lock()
        self._next_at = 0.0
        self._busy = False
        self.rows = None
        self.fetched_at = None
        self.error = None

    def refresh(self, *args, wait=False):
        with self._lock:
            now = self._clock()
            if self._busy or now < self._next_at:
                return
            self._busy = True
        worker = threading.Thread(target=self._run, args=(now, *args), daemon=True, name="roof-forecast")
        worker.start()
        if wait:
            worker.join()

    def _run(self, now, *args):
        try:
            rows = self._load(now, *args)
            with self._lock:
                self.rows, self.fetched_at, self.error = rows, now, None
                self._next_at = now + REFRESH_S
        except Exception as e:
            with self._lock:
                self.error = str(e) or type(e).__name__
                self._next_at = now + RETRY_S
        finally:
            with self._lock:
                self._busy = False


_csc = _Source(lambda now, tz, lat, lon: decode_csc(fetch_csc(), now, tz))
_meteo = _Source(lambda now, tz, lat, lon: decode_open_meteo(fetch_open_meteo(lat, lon)))


def _night_label(key, tz):
    return datetime.fromisoformat(key).strftime("%a")


def outlook(settings, clock, tz, now=None, csc_rows=None, meteo_rows=None) -> dict | None:
    """Tonight's and the next nights' roof outlook. Rows are taken from the background
    sources unless given; starts a refresh when they're due and never waits on it."""
    if not (settings.lat or settings.lon):
        return None
    now = time.time() if now is None else now
    if csc_rows is None and meteo_rows is None:
        for source in (_csc, _meteo):
            source.refresh(tz, settings.lat, settings.lon)
        csc_rows, meteo_rows = _csc.rows, _meteo.rows
    if not csc_rows and not meteo_rows:
        return {"pending": True, "sources": _source_notes()}

    nights = []
    key = current_night(now, settings)
    for i in range(NIGHTS):
        night = (datetime.fromisoformat(key) + timedelta(days=i)).date().isoformat()
        times = night_times(night, settings.lat, settings.lon, settings.sun_max_deg)
        if times["sunset"] is None or times["sunrise"] is None:
            continue
        start, end = times["sunset"] + ROOF_OPEN_OFFSET_S, times["sunrise"] + ROOF_CLOSE_OFFSET_S
        # Tonight, only what's still ahead: the record already says what happened.
        hours = rate_hours(max(start, now) if i == 0 else start, end, csc_rows, meteo_rows)
        if not hours:
            continue
        entry = {
            "night": night,
            "label": "Tonight" if i == 0 else _night_label(night, tz),
            "start": start,
            "end": end,
            **verdict(hours, clock),
            "hours": [
                {
                    "start": h["start"],
                    "end": h["end"],
                    "state": STATE_WORDS[h["state"]],
                    "why": ", ".join(dict.fromkeys(r[3] for r in h["reasons"])),
                }
                for h in hours
            ],
        }
        if i == 0:
            entry["dew"] = dew_note(meteo_rows, max(start, now), end, clock)
        nights.append(entry)
    return {"pending": False, "nights": nights, "sources": _source_notes()}


def open_chance(settings, tz, now=None, csc_rows=None, meteo_rows=None):
    """A function of time giving the chance the roof is open then, for the planner.

    Rows are taken from the background sources unless given; it never waits on a
    fetch. Hours no model covers, and hours far ahead, get the typical chance.
    """
    now = time.time() if now is None else now
    typical = max(0.0, min(1.0, settings.typical_open_chance))
    if csc_rows is None and meteo_rows is None:
        for source in (_csc, _meteo):
            source.refresh(tz, settings.lat, settings.lon)
        csc_rows, meteo_rows = _csc.rows, _meteo.rows
    rows = list(csc_rows or ()) + list(meteo_rows or ())
    rated = {}
    if rows:
        first = min(r["at"] for r in rows)
        last = max(r["at"] for r in rows) + 3600
        for h in rate_hours(first, last, csc_rows, meteo_rows):
            rated[int(h["start"] // 3600)] = OPEN_CHANCE[h["state"]]

    def chance(when: float) -> float:
        forecast = rated.get(int(when // 3600))
        if forecast is None:
            return typical
        lead = when - now
        trust = 1.0 if lead <= TRUST_FULL_S else max(0.0, 1 - (lead - TRUST_FULL_S) / (TRUST_NONE_S - TRUST_FULL_S))
        return trust * forecast + (1 - trust) * typical

    chance.forecast_until = max(rated) * 3600 + 3600 if rated else None
    return chance


def _source_notes() -> list[str]:
    notes = []
    for name, source in (("ClearDarkSky", _csc), ("Open-Meteo", _meteo)):
        if source.error and source.rows is None:
            notes.append(f"{name} unavailable")
        elif source.error:
            notes.append(f"{name} kept from an earlier fetch")
    return notes


_memo_lock = threading.Lock()
_memo = {"key": None, "value": None}
MEMO_S = 300


def latest(settings, clock, tz, now=None) -> dict | None:
    """outlook() for pages that poll: worked out again only when a model has new rows
    or every MEMO_S, and starts a background refresh when one is due."""
    now = time.time() if now is None else now
    if not (settings.lat or settings.lon):
        return None
    for source in (_csc, _meteo):
        source.refresh(tz, settings.lat, settings.lon)
    key = (settings.lat, settings.lon, settings.sun_max_deg, _csc.fetched_at, _meteo.fetched_at, int(now // MEMO_S))
    with _memo_lock:
        if _memo["key"] == key:
            return _memo["value"]
    if not _csc.rows and not _meteo.rows:
        return {"pending": True, "sources": _source_notes()}
    value = outlook(settings, clock, tz, now, _csc.rows or [], _meteo.rows or [])
    with _memo_lock:
        _memo.update(key=key, value=value)
    return value
