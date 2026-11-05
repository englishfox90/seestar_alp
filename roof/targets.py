"""Altitude of schedule targets, for the minimum-altitude gate."""

from datetime import datetime, timezone

from astropy.time import Time

from device import horizon
from device.seestar_util import Util


def target_altitude_deg(params: dict, lat: float, lon: float, when: datetime | None = None):
    """Current altitude of a schedule item's target, or None if it has no fixed one."""
    ra, dec = params.get("ra"), params.get("dec")
    if ra is None or dec is None or (ra == -1 and dec == -1):
        return None  # "wherever the scope is pointing"
    coord = Util.parse_coordinate(bool(params.get("is_j2000", True)), ra, dec)
    when = when or datetime.now(timezone.utc)
    altitude, _azimuth = horizon.altaz(coord, lat, lon, Time(when))
    return altitude
