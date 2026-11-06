"""What the sky looks like from the site on a given night, for the Targets sky view.

Read-only, and never asks the scope. It gives the browser the sidereal time through the
night's dark hours, so the page can work out any point of the sky without another request.
"""

import math
import time
from datetime import datetime, timezone

import ephem

from roof import config as roof_config
from roof.planner import night_times
from roof.sun import night_key

STEP_S = 900
# Sky that can't stay above the limit this long isn't worth planning on.
MIN_USABLE_HOURS = 1.0
MAX_DAYS = 366


def _sidereal_deg(lat: float, lon: float, when: float) -> float:
    observer = ephem.Observer()
    observer.lat, observer.lon = str(lat), str(lon)
    observer.pressure = 0
    observer.date = ephem.Date(datetime.fromtimestamp(when, timezone.utc).replace(tzinfo=None))
    return math.degrees(observer.sidereal_time())


def _horizon_samples() -> list | None:
    """The obstruction altitude every 10 degrees of azimuth, or None without a profile."""
    from device import horizon
    from device.config import Config

    profile, _error = horizon.configured_profile()
    if profile is None:
        return None
    margin = float(Config.horizon_margin or 0.0)
    return [round(profile.altitude_at(az) + margin, 1) for az in range(0, 360, 10)]


def sky_window(days: int, clock, now: float | None = None, settings=None) -> dict:
    """The dark hours of the night ``days`` from now. clock(ts) formats a site time."""
    settings = settings or roof_config.load()
    now = time.time() if now is None else now
    known = bool(settings.lat or settings.lon)
    key = night_key(now + days * 86400, settings.lon)
    reply = {
        "known": known,
        "night": key,
        "lat": settings.lat,
        "floor": settings.min_target_alt_deg,
        "min_hours": MIN_USABLE_HOURS,
        "step_minutes": STEP_S // 60,
        "horizon": None,
        "steps": [],
    }
    if not known:
        return reply
    reply["horizon"] = _horizon_samples()
    times = night_times(key, settings.lat, settings.lon, settings.sun_max_deg)
    start, end = times["dark_start"], times["dark_end"]
    if start is None or end is None:
        return reply
    when = start
    while when <= end:
        reply["steps"].append(
            {"lst": round(_sidereal_deg(settings.lat, settings.lon, when), 2), "at": clock(when)}
        )
        when += STEP_S
    return reply
