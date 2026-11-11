"""Read-only data for the Tonight screen: roof history, morning summary, full image.

Nothing here sends anything to the scope or the safety monitor. It reads the
history and session log from memory, and the image the dashboard already holds.
"""

import json
import time

import falcon

from roof import config as roof_config
from roof import dashboard, site_status
from roof.automation import get_automation, night_history, session_log, target_queue
from roof.sessions import _PANEL_SUFFIX, base_name, counts
from roof.sun import night_key


def _kept_percent(stacked, dropped):
    # The complement of the dashboard's drop_percent, so the two always add up.
    total = stacked + dropped
    return 100 - round(dropped * 100 / total) if total else None


def _goal(target, log) -> dict | None:
    if target is None:
        return None
    return {
        "have_hours": round(target.prior_hours + log.progress(target).counted_hours, 2),
        "goal_hours": target.goal_hours,
        "text": dashboard.goal_text(target, log),
    }


def _not_counted(target, row) -> bool:
    """A framed run that SessionLog.progress leaves out."""
    return target is not None and not counts(target, row)


def night_summary(night: str, log, targets) -> dict:
    """What a night earned, from the session log alone. {} if it imaged nothing.

    Hours are frames stacked times exposure, as everywhere else in the project.
    hours_not_counted is the part of hours that doesn't count toward the goal yet,
    and goal is None for a target that isn't on the list.
    """
    rows = [r for r in log.rows() if r.get("night") == night] if log else []
    if not rows:
        return {}
    known = {base_name(t.name): t for t in targets or []}
    earned = {}
    for row in sorted(rows, key=lambda r: r["started"]):
        target = known.get(row["target"])
        entry = earned.setdefault(
            row["target"],
            {
                "name": target.name if target else _PANEL_SUFFIX.sub("", row["name"]),
                "hours": 0.0,
                "hours_not_counted": 0.0,
                "stacked": 0,
                "dropped": 0,
            },
        )
        hours = row["stacked"] * (row.get("exposure_s") or 0) / 3600
        entry["hours"] += hours
        if _not_counted(target, row):
            entry["hours_not_counted"] += hours
        entry["stacked"] += row["stacked"]
        entry["dropped"] += row.get("dropped") or 0
    stacked = sum(e["stacked"] for e in earned.values())
    dropped = sum(e["dropped"] for e in earned.values())
    total_hours = sum(e["hours"] for e in earned.values())
    return {
        "night": night,
        "targets": [
            {
                **entry,
                "hours": round(entry["hours"], 2),
                "hours_not_counted": round(entry["hours_not_counted"], 2),
                "kept_percent": _kept_percent(entry["stacked"], entry["dropped"]),
                "goal": _goal(known.get(key), log),
            }
            for key, entry in earned.items()
        ],
        "total_hours": round(total_hours, 2),
        "total_stacked": stacked,
        "total_dropped": dropped,
        "kept_percent": _kept_percent(stacked, dropped),
    }


def night_data(now: float) -> dict:
    """The current night's roof periods and summary. Reads memory and files only."""
    lon = roof_config.load().lon
    night = night_key(now, lon)
    history = night_history()
    data = {
        "night": night,
        "now": now,
        "roof": {
            "recording_since": history.recording_since() if history else None,
            "closed": history.closed_periods(night, lon, now) if history else [],
        },
        "summary": {},
    }
    # Optional, like the dashboard's target list: a bad row mustn't lose the roof.
    try:
        data["summary"] = night_summary(night, session_log(), target_queue().all())
    except Exception as e:
        data["summary_error"] = f"Could not read the session log: {e}"
    return data


class DashboardNight:
    @staticmethod
    def on_get(req, resp, telescope_id=1):
        resp.content_type = "application/json"
        resp.set_header("Cache-Control", "no-store")
        resp.text = json.dumps(night_data(time.time()))


class DashboardSite:
    """Starfront's public status, for a person to read. Decides nothing."""

    @staticmethod
    def on_get(req, resp, telescope_id=1):
        data = site_status.site_data(
            roof_config.load(), get_automation().status().get("safe"), dashboard.site_clock
        )
        resp.content_type = "application/json"
        resp.set_header("Cache-Control", "no-store")
        resp.text = json.dumps(data)


class DashboardImageView:
    """The full-size stack for a viewer: image.jpg?full=true without the download."""

    @staticmethod
    def on_get(req, resp, telescope_id=1):
        image = dashboard._store(telescope_id).latest(dashboard._imager(telescope_id))
        if not image:
            raise falcon.HTTPNotFound(description="no image yet")
        resp.content_type = "image/jpeg"
        resp.set_header("Cache-Control", "no-store")
        resp.data = image["full"]


PAGES = {
    "dashboard/night.json": DashboardNight,
    "dashboard/site.json": DashboardSite,
    "dashboard/image-view.jpg": DashboardImageView,
}
