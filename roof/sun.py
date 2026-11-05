"""Solar altitude for the darkness gate (NOAA low-precision formula, stdlib only)."""

import math
from datetime import datetime, timedelta, timezone


def sun_altitude_deg(lat: float, lon: float, when: datetime | None = None) -> float:
    when = when or datetime.now(timezone.utc)
    when = when.astimezone(timezone.utc)
    y, m = when.year, when.month
    d = when.day + (when.hour + (when.minute + when.second / 60.0) / 60.0) / 24.0
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    jd = int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + b - 1524.5
    n = jd - 2451545.0
    g = math.radians((357.529 + 0.98560028 * n) % 360.0)
    q = (280.459 + 0.98564736 * n) % 360.0
    ecl_lon = math.radians((q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g)) % 360.0)
    e = math.radians(23.439 - 0.00000036 * n)
    dec = math.asin(math.sin(e) * math.sin(ecl_lon))
    ra = math.atan2(math.cos(e) * math.sin(ecl_lon), math.cos(ecl_lon))
    gmst = (280.46061837 + 360.98564736629 * n) % 360.0
    ha = math.radians((gmst + lon) % 360.0) - ra
    phi = math.radians(lat)
    return math.degrees(
        math.asin(
            math.sin(phi) * math.sin(dec) + math.cos(phi) * math.cos(dec) * math.cos(ha)
        )
    )


def night_key(when_ts: float, lon: float) -> str:
    """Name of the observing night containing ``when_ts``.

    Nights change at local solar noon, so one night's date never splits across
    midnight and the boundary needs no time zone or DST handling.
    """
    utc = datetime.fromtimestamp(when_ts, timezone.utc)
    solar = utc + timedelta(hours=lon / 15.0) - timedelta(hours=12)
    return solar.date().isoformat()
