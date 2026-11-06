"""What Starfront publishes about the site: https://status.starfront.space

For a person to read. Nothing here decides anything: the automation still answers only to
the roof monitor. Fetched only while a page is open, and a failed fetch keeps the last answer.
"""

import threading
import time
from datetime import datetime

import requests

from roof.planner import night_times
from roof.sky_window import current_night

ROOF_STATE_URL = "https://alpaca-api.tx.starfront.space/api/v1/roof/state"
ANNOUNCEMENTS_URL = "https://starfront-announcements.slack-announcer.workers.dev/announcements.json"
LINKS = {
    "chart_image": "https://www.cleardarksky.com/c/StrfrntObsTXcsk.gif?c=644447",
    "chart_page": "https://www.cleardarksky.com/c/StrfrntObsTXkey.html",
    "status": "https://status.starfront.space",
    "satellite": "https://www.star.nesdis.noaa.gov/GOES/sector.php?sat=G19&sector=sp",
    "radar": "https://radar.weather.gov/station/ksjt/standard",
    "allsky": "https://files-api.tx.starfront.space/status-assets-public/building-0009/allsky/images/image.jpg",
}
CAMERA_URL = "https://files-api.tx.starfront.space/status-assets-public/building-{:04d}/current.jpg"
# Starfront's own offsets from sunset and sunrise, from its status page.
ROOF_OPEN_OFFSET_S = -15 * 60
ROOF_CLOSE_OFFSET_S = 5 * 60
FETCH_INTERVAL_S = 60
TIMEOUT_S = 4
# Its own feed changes state within a minute or so. Older than this, say it may be out of date.
STALE_AFTER_S = 15 * 60
MAX_NEWS = 3
_TONES = {"info": "neutral", "good": "ok", "warning": "attention", "alert": "danger"}


class _Feed:
    def __init__(self, url, clock=time.time, get=None):
        self._url = url
        self._clock = clock
        self._get = get or requests.get
        self._lock = threading.Lock()
        self._checked_at = None
        self._data = None
        self.ok = False

    def latest(self):
        with self._lock:
            now = self._clock()
            if self._checked_at is None or now - self._checked_at >= FETCH_INTERVAL_S:
                self._checked_at = now
                try:
                    reply = self._get(self._url, timeout=TIMEOUT_S)
                    reply.raise_for_status()
                    data = reply.json()
                    if not isinstance(data, list):
                        raise ValueError("not a list")
                    self._data, self.ok = data, True
                except Exception:
                    self.ok = False
            return self._data


_roof_feed = _Feed(ROOF_STATE_URL)
_news_feed = _Feed(ANNOUNCEMENTS_URL)


def _timestamp(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def building_roof(rows, number, clock, now) -> dict | None:
    for row in rows or ():
        if isinstance(row, dict) and row.get("device_number") == number:
            updated = _timestamp(row.get("state_update"))
            return {
                "name": str(row.get("name") or f"Building {number}"),
                "open": row.get("is_open") is True,
                "updated": clock(updated) if updated else None,
                "stale": updated is None or now - updated > STALE_AFTER_S,
            }
    return None


def news_items(rows, clock) -> list:
    items = []
    for row in rows or ():
        if isinstance(row, dict) and (row.get("title") or row.get("body")):
            when = _timestamp(row.get("date"))
            items.append(
                {
                    "at": when or 0,
                    "title": str(row.get("title") or ""),
                    "body": str(row.get("body") or ""),
                    "tone": _TONES.get(row.get("level"), "neutral"),
                    "when": clock(when) if when else "",
                }
            )
    items.sort(key=lambda item: -item["at"])
    return [{k: v for k, v in item.items() if k != "at"} for item in items[:MAX_NEWS]]


def roof_schedule(settings, clock, now) -> dict | None:
    if not (settings.lat or settings.lon):
        return None
    key = current_night(now, settings)
    times = night_times(key, settings.lat, settings.lon, settings.sun_max_deg)
    if times["sunset"] is None or times["sunrise"] is None:
        return None
    return {
        "open": clock(times["sunset"] + ROOF_OPEN_OFFSET_S),
        "close": clock(times["sunrise"] + ROOF_CLOSE_OFFSET_S),
    }


def links(settings) -> dict:
    found = dict(LINKS)
    if settings.site_building:
        found["camera"] = CAMERA_URL.format(settings.site_building)
    return found


def site_data(settings, safe, clock, now=None) -> dict:
    now = time.time() if now is None else now
    building = None
    if settings.site_building:
        building = building_roof(_roof_feed.latest(), settings.site_building, clock, now)
    # Only one way round is a real disagreement: an open roof reads unsafe for good reasons.
    disagree = bool(building and not building["open"] and not building["stale"] and safe is True)
    return {
        "building": building,
        "schedule": roof_schedule(settings, clock, now),
        "disagree": disagree,
        "news": news_items(_news_feed.latest(), clock),
    }
