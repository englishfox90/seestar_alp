"""The /targets page: the target list, progress, and what the planner would do.

Nothing here commands the scope, and it reads it only to import a plan. Running from
this list is chosen on the /roof page.
"""

import os
import time
from datetime import datetime

import falcon
from astropy.coordinates import Angle

from roof import config as roof_config
from roof import planner, schedule_tools
from roof import queue as target_queue
from roof.automation import session_log
from roof.web import _Action, _form, _front

FORECAST_NIGHTS = 14


def _base() -> str:
    from device.config import Config

    return os.path.dirname(Config.path_to_dat)


def _queue() -> target_queue.TargetQueue:
    return target_queue.TargetQueue(os.path.join(_base(), "roof_targets.json"))


def _clock(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _night_label(key: str) -> str:
    return datetime.fromisoformat(key).strftime("%a %b %-d")


def _framing(target) -> str:
    if target.framed:
        return f"Framed {target.mosaic_scale:g}x at {target.mosaic_angle:g}°"
    return "Single frame"


def _plan_row(plan, names) -> dict:
    return {
        "label": _night_label(plan.key),
        "dark": f"{_clock(plan.dark_start)} to {_clock(plan.dark_end)}" if plan.dark_start else None,
        "moon": round(plan.moon_percent),
        "blocks": [
            {
                "start": _clock(b.start),
                "end": _clock(b.end),
                "name": b.target.name,
                "framing": _framing(b.target),
                "wall": round((b.end - b.start) / 3600, 1),
                "expected": round(b.expected_hours, 1),
            }
            for b in plan.blocks
        ],
        "skipped": [
            {"name": names[target_id], "why": why}
            for target_id, why in plan.skipped.items()
            if target_id in names
        ],
    }


def _context() -> dict:
    settings = roof_config.load()
    targets = _queue().all()
    log = session_log()
    context = {
        "settings": settings,
        "roof_source": schedule_tools.label(settings.schedule),
        "uses_list": settings.schedule == schedule_tools.TARGET_LIST,
        "rows": [], 
        "tonight": None,
        "nights": [],
        "site_known": True,
    }
    if not (settings.lat or settings.lon):
        context["site_known"] = False
        return context
    plans = planner.forecast(
        targets, log, planner.config_from_settings(settings), time.time(), FORECAST_NIGHTS
    )
    finished = planner.finish_nights(plans, targets, log)
    names = {t.id: t.name for t in targets}
    for target in targets:
        progress = log.progress(target)
        have = target.prior_hours + progress.counted_hours
        context["rows"].append(
            {
                "target": target,
                "framing": _framing(target),
                "have": round(have, 1),
                "partial": round(progress.partial_hours, 1),
                "percent": min(100, round(100 * have / target.goal_hours)) if target.goal_hours else 100,
                "sessions": progress.sessions,
                "rate": round(progress.measured_rate * 100) if progress.measured_rate else None,
                "finish": _night_label(finished[target.id]) if target.id in finished else None,
            }
        )
    rows = [_plan_row(plan, names) for plan in plans]
    if rows:
        context["tonight"] = rows[0]
        context["nights"] = rows[1:]
    return context


class QueuePage:
    @staticmethod
    def on_get(req, resp, telescope_id=1):
        front = _front()
        context = dict(front.get_context(telescope_id, req))
        context["partial_path"] = "targets"
        front.render_template(
            req,
            resp,
            "target_list.html",
            priorities=target_queue.PRIORITIES,
            import_schedules=[
                name
                for name in schedule_tools.list_schedules()
                if name != schedule_tools.PLAN_FILE
            ],
            page_root=f"/{telescope_id}/targets",
            roof_url=f"/{telescope_id}/roof",
            **_context(),
            **context,
        )


class _QueueAction(_Action):
    redirect_to = "targets"


def _positive(form, key, default=None, maximum=None) -> float:
    try:
        value = float(form.get(key, default))
    except (TypeError, ValueError):
        raise falcon.HTTPBadRequest(description=f"{key} must be a number") from None
    if value <= 0 or (maximum is not None and value > maximum):
        raise falcon.HTTPBadRequest(description=f"{key} is out of range")
    return value


def _coordinate(text, unit, low, high) -> float:
    text = str(text or "").strip()
    try:
        value = float(text)
    except ValueError:
        try:
            angle = Angle(text, unit=unit)
        except Exception:
            raise falcon.HTTPBadRequest(description=f"cannot read coordinate {text!r}") from None
        value = angle.hour if unit == "hour" else angle.deg
    if not low <= value <= high:
        raise falcon.HTTPBadRequest(description=f"coordinate {text!r} is out of range")
    return round(float(value), 4)


class QueueAdd(_QueueAction):
    @staticmethod
    def act(req):
        form = _form(req)
        name = " ".join(str(form.get("name", "")).split())
        if not name:
            raise falcon.HTTPBadRequest(description="a target needs a name")
        added, _ = _queue().merge(
            [
                {
                    "name": name,
                    "ra": _coordinate(form.get("ra"), "hour", 0.0, 24.0),
                    "dec": _coordinate(form.get("dec"), "deg", -90.0, 90.0),
                    "lp_filter": form.get("lp_filter") == "on",
                    "source": "added by hand",
                }
            ],
            goal_hours=_positive(form, "goal_hours", target_queue.DEFAULT_GOAL_HOURS),
        )
        return f"Added {name}." if added else f"{name} was already listed; updated its coordinates."


class QueueImport(_QueueAction):
    @staticmethod
    def act(req):
        form = _form(req)
        goal = _positive(form, "goal_hours", target_queue.DEFAULT_GOAL_HOURS)
        source = form.get("source", "scope")
        if source == "scope":
            from device import telescope

            device = telescope.seestar_dev.get(roof_config.load().device_num)
            if device is None or not device.is_connected:
                return "Import failed: the scope is not connected."
            plan = schedule_tools.loaded_plan(device)
            if not plan:
                return (
                    "Import failed: the scope isn't holding a plan. It only has one "
                    "while the Seestar app has that plan loaded."
                )
            found = target_queue.targets_from_plan(plan, plan.get("plan_name") or "Seestar plan")
            label = plan.get("plan_name") or "the scope's plan"
        else:
            path = schedule_tools.resolve(source)
            if not path:
                raise falcon.HTTPBadRequest(description="unknown schedule")
            items, errors, _ = schedule_tools.load(path)
            if errors:
                return f"Import failed: {'; '.join(errors)}"
            found = target_queue.targets_from_schedule(items, source)
            label = source
        added, updated = _queue().merge(found, goal_hours=goal)
        return f"From {label}: added {added}, updated {updated}."


class QueueUpdate(_QueueAction):
    @staticmethod
    def act(req):
        form = _form(req)
        priority = form.get("priority", "normal")
        if priority not in target_queue.PRIORITIES:
            raise falcon.HTTPBadRequest(description="unknown priority")
        try:
            prior = float(form.get("prior_hours", 0) or 0)
        except ValueError:
            raise falcon.HTTPBadRequest(description="prior_hours must be a number") from None
        if prior < 0:
            raise falcon.HTTPBadRequest(description="prior_hours is out of range")
        changed = _queue().update(
            form.get("id", ""),
            goal_hours=_positive(form, "goal_hours"),
            min_session_hours=_positive(form, "min_session_hours", maximum=14),
            prior_hours=prior,
            priority=priority,
            enabled=form.get("enabled") == "on",
        )
        return "Saved." if changed else "That target is no longer listed."


class QueueRemove(_QueueAction):
    @staticmethod
    def act(req):
        removed = _queue().remove(_form(req).get("id", ""))
        return "Removed. Its recorded sessions are kept." if removed else "That target is no longer listed."


PAGES = {
    "targets": QueuePage,
    "targets/add": QueueAdd,
    "targets/import": QueueImport,
    "targets/update": QueueUpdate,
    "targets/remove": QueueRemove,
}
