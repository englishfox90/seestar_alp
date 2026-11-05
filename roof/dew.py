"""Temperature and dew point at the site, for the automatic dew heater.

Reads every configured source and keeps the smallest margin between temperature
and dew point, so a disagreement between forecasts errs toward heating. These are
regional estimates, not a sensor at the scope.
"""

import math
import time
from dataclasses import dataclass

import requests

TIMEOUT_S = 5
CACHE_S = 300


@dataclass(frozen=True)
class DewReading:
    temp_c: float
    dew_point_c: float
    source: str

    @property
    def margin_c(self) -> float:
        return self.temp_c - self.dew_point_c


def dew_point_c(temp_c: float, humidity_pct: float) -> float:
    """Magnus approximation."""
    a, b = 17.27, 237.7
    alpha = (a * temp_c) / (b + temp_c) + math.log(humidity_pct / 100.0)
    return (b * alpha) / (a - alpha)


def read_sentinel(url: str) -> DewReading | None:
    meta = requests.get(url, timeout=TIMEOUT_S).json().get("metadata") or {}
    temp = float(str(meta["WEATHER_TEMP"]).split("°")[0])
    humidity = float(str(meta["WEATHER_HUMIDITY"]).rstrip("%"))
    if humidity <= 0:
        return None
    return DewReading(temp, dew_point_c(temp, humidity), "Sentinel weather")


def read_open_meteo(lat: float, lon: float) -> DewReading | None:
    current = (
        requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,dew_point_2m",
            },
            timeout=TIMEOUT_S,
        )
        .json()
        .get("current")
    )
    return DewReading(
        float(current["temperature_2m"]), float(current["dew_point_2m"]), "Open-Meteo"
    )


class DewMonitor:
    """Cached so the loop's tick never waits on the network more than occasionally."""

    def __init__(self, clock=time.time, readers=None):
        self._clock = clock
        self._readers = readers
        self._at = 0.0
        self._reading = None

    def read(self, s) -> DewReading | None:
        now = self._clock()
        if self._at and now - self._at < CACHE_S:
            return self._reading
        self._at = now
        readers = self._readers or [
            lambda: read_sentinel(s.dew_sentinel_url),
            lambda: read_open_meteo(s.lat, s.lon),
        ]
        readings = []
        for reader in readers:
            try:
                reading = reader()
            except Exception:
                reading = None
            if reading is not None:
                readings.append(reading)
        self._reading = min(readings, key=lambda r: r.margin_c) if readings else None
        return self._reading
