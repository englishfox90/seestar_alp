"""The fragments and images behind the Tonight page (roof/tonight.py).

Nothing here commands the scope. The only message the image routes can send is
get_stacked_img on the imaging port, which reads the current stack, and only
while a browser is polling for the image. The autofocus curve only listens.
The pier camera comes from PFR Sentinel, not the scope.
"""

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import cv2
import falcon
import numpy as np
import pydash
import requests

from roof import config as roof_config
from roof import focus, planner, schedule_tools
from roof.automation import get_automation, session_log, target_queue
from roof.sessions import base_name
from roof.sun import night_key
from roof.targets import target_altitude_deg

PREVIEW_WIDTH = 720
PREVIEW_QUALITY = 80
FULL_QUALITY = 92
# A stack image is large and crosses the scope's Wi-Fi, so never ask more often.
MIN_FETCH_INTERVAL_S = 30
# A request's label only describes a reply that comes back about then. Images can also
# arrive without one, and go unseen until a browser polls hours later.
PENDING_MAX_AGE_S = 2 * MIN_FETCH_INTERVAL_S
# How long after asking the scope for a frame the page keeps looking for it every few seconds.
PENDING_QUICK_S = 20
PIER_PREVIEW_WIDTH = 1024
PIER_FETCH_INTERVAL_S = 30
PIER_TIMEOUT_S = 5
# Sentinel's own default for calling an image stale.
PIER_STALE_S = 300


def _front():
    # Imported late: front.app calls roof.web.install() while starting.
    import front.app as front_app

    return front_app


def _hms(seconds):
    return str(timedelta(seconds=int(seconds)))


def _duration(seconds):
    hours, remainder = divmod(int(seconds), 3600)
    minutes = remainder // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    return f"{hours}h" if hours else f"{minutes}m"


def is_stacking(view) -> bool:
    return (
        isinstance(view, dict)
        and view.get("state") == "working"
        and view.get("stage") == "Stack"
        and isinstance(view.get("Stack"), dict)
    )


def _current_item(schedule):
    current = pydash.get(schedule, "current_item_id")
    for item in pydash.get(schedule, "list") or []:
        if current and item.get("schedule_item_id") == current:
            return item
    return None


def session_summary(view, schedule=None) -> dict:
    """Reduce get_view_state and get_schedule to what the page shows."""
    view = view if isinstance(view, dict) else {}
    state = view.get("state")
    summary = {
        "active": state == "working",
        "state": state or "idle",
        "mode": view.get("mode"),
        "stage": view.get("stage"),
        "target": view.get("target_name"),
        "gain": view.get("gain"),
        "stack": None,
        "planned": None,
    }
    if state == "working":
        stage_detail = view.get(view.get("stage") or "")
        if isinstance(stage_detail, dict) and view.get("stage") != "Stack":
            summary["substage"] = stage_detail.get("stage")
    if is_stacking(view):
        stack = view["Stack"]
        stacked = stack.get("stacked_frame") or 0
        dropped = stack.get("dropped_frame") or 0
        exp_ms = pydash.get(stack, "Exposure.exp_ms")
        total = stacked + dropped
        summary["stack"] = {
            "stacked": stacked,
            "dropped": dropped,
            "drop_percent": round(dropped * 100 / total) if total else None,
            "exposure_s": round(exp_ms / 1000, 1) if exp_ms else None,
            "integration": _hms(stacked * exp_ms / 1000) if exp_ms else None,
            "elapsed": _hms((stack.get("lapse_ms") or 0) / 1000),
        }
    params = (_current_item(schedule) or {}).get("params") or {}
    if summary["active"] and params.get("panel_time_sec"):
        summary["planned"] = _duration(params["panel_time_sec"])
    return summary


def plan_rows(status, session) -> list:
    """The selected schedule's targets, marked done (passed), now or next.

    "done" only means the scheduler has moved past it: a target too low at the
    start of the night is skipped, so the page says "Passed", not "Done".
    """
    rows = []
    current = status.get("target") or session.get("target")
    names = [t.get("name") for t in status.get("targets") or []]
    running = current in names and status.get("sched_state") == "working"
    position = names.index(current) if running else None
    for index, target in enumerate(status.get("targets") or []):
        progress = None
        if running:
            if index < position:
                progress = "done"
            elif index == position:
                progress = "now"
            else:
                progress = "next"
        rows.append({**target, "progress": progress})
    return rows


def goal_text(target, log) -> str:
    """Hours toward a target's goal. Runs too short to count yet are shown apart."""
    progress = log.progress(target)
    have = target.prior_hours + progress.counted_hours
    text = f"{have:.1f} of {target.goal_hours:g} h"
    if progress.partial_hours >= 0.05:
        text += f", plus {progress.partial_hours:.1f} h not counted, framing not covered"
    return text


def site_clock(timestamp) -> str:
    """Observatory time, 24-hour with no seconds. The browser's zone may differ."""
    return datetime.fromtimestamp(timestamp, _front().site_timezone()).strftime("%H:%M")


_clock = site_clock


def _list_summary(status, targets, log, settings, now) -> dict | None:
    """The plan running now, or while not running, what a start now would do."""
    night = night_key(now, settings.lon)
    if status.get("sched_state") == "working":
        running = planner.read_plan_times(schedule_tools.schedule_dir(), night)
        if running:
            return running
    cfg = planner.config_from_settings(settings)
    return planner.plan_summary(planner.forecast(targets, log, cfg, now, 1)[0], targets)


def target_list_plan(status, session, targets, log, settings, now, altitude=None) -> dict:
    """Tonight's plan from the target list, ready for the page."""
    summary = _list_summary(status, targets, log, settings, now) or {"blocks": [], "skipped": []}
    by_id = {t.id: t for t in targets}
    altitude = altitude or _altitude_now
    working = status.get("sched_state") == "working"
    position = (status.get("target_index") or 0) - 1 if working else None
    rows = []
    for index, block in enumerate(summary["blocks"]):
        progress = None
        if position is not None and position >= 0:
            progress = "done" if index < position else "now" if index == position else "next"
        target = by_id.get(block["id"])
        rows.append(
            {
                "name": block["name"],
                "kind": block["kind"],
                "altitude": altitude(block, settings, now),
                "ok": True,  # the planner chose its time, so being low now doesn't matter
                "progress": progress,
                "when": f"{_clock(block['start'])} to {_clock(block['end'])}",
                "start": block["start"],
                "end": block["end"],
                "goal": goal_text(target, log) if target else None,
            }
        )
    return {
        "rows": rows,
        "skipped": [{"name": s["name"], "why": s["why"]} for s in summary["skipped"]],
    }


def _altitude_now(block, settings, now):
    try:
        return target_altitude_deg(
            {"ra": block["ra"], "dec": block["dec"], "is_j2000": True},
            settings.lat,
            settings.lon,
            datetime.fromtimestamp(now, timezone.utc),
        )
    except Exception:
        return None


def session_goal(session, targets, log) -> str | None:
    """Goal progress for the target being imaged, if it's on the list."""
    name = session.get("target")
    if not name:
        return None
    key = base_name(name)
    for target in targets:
        if base_name(target.name) == key:
            return goal_text(target, log)
    return None


def list_details(status, session, now=None) -> dict:
    """Everything the page shows from the target list. It's optional, so a problem
    here falls back to the plain plan rather than breaking the dashboard."""
    if not status.get("uses_target_list"):
        return {}
    now = now or time.time()
    try:
        targets = target_queue().all()
        log = session_log()
        details = target_list_plan(status, session, targets, log, roof_config.load(), now)
        details["goal"] = session_goal(session, targets, log)
        return details
    except Exception as e:
        return {"error": f"Could not read the target list: {e}"}


def _number(text):
    match = re.search(r"-?\d+(\.\d+)?", str(text))
    return float(match.group()) if match else None


def _clamp_percent(value):
    return max(0, min(100, int(value)))


def _tone_high_is_good(value, danger, warning):
    if value <= danger:
        return "text-danger"
    return "text-warning" if value <= warning else "text-success"


def _storage_row(value):
    size = _number(value)
    text = str(value)
    if size is None:
        return {"tone": ""}
    if " GB" not in text and " TB" not in text:
        return {"tone": "text-danger"}
    gigabytes = size * 1000 if " TB" in text else size
    return {"tone": _tone_high_is_good(gigabytes, 5, 10)}


def _battery_row(value):
    level = _number(value)
    if level is None:
        return {"tone": ""}
    return {
        "tone": _tone_high_is_good(level, 5, 15),
        "bar": _clamp_percent(level),
        "value": f"{level:g}%",
    }


def _wifi_row(value):
    if "dBm" not in str(value):
        return {"tone": ""}
    dbm = _number(value)
    return {
        "tone": _tone_high_is_good(dbm, -80, -70),
        "bar": _clamp_percent((dbm + 90) * 100 / 40),
    }


def _temperature_row(value):
    degrees = _number(value)
    return {"tone": "", "value": f"{degrees:.1f} °C"} if degrees is not None else {}


def _on_off_row(value):
    return {"tone": "", "value": {True: "On", False: "Off"}.get(value, value)}


# Label on the page, key in front.app.get_device_state, how to present it.
DEVICE_ROWS = [
    ("Battery", "Battery %", _battery_row),
    ("Charging", "Charge Status", None),
    ("Scope temperature", "Temperature Sensor", _temperature_row),
    ("Free storage", "Free Storage", _storage_row),
    ("Wi-Fi signal", "Wi-Fi Signal", _wifi_row),
    ("Dew heater", "Heater?", _on_off_row),
    ("Mount mode", "Mount Mode", None),
]


def device_rows(stats) -> list:
    rows = []
    for label, key, present in DEVICE_ROWS:
        value = (stats or {}).get(key)
        if value is None or value == "":
            continue
        row = {"key": label, "value": value, "tone": "", "bar": None}
        if present:
            row.update(present(value))
        rows.append(row)
    return rows


# The order every target has followed on this scope. Focus and Darks can be
# skipped, but nothing runs out of order.
STEPS = [
    ("ready", "Ready"),
    ("open", "Open"),
    ("slew", "Slew"),
    ("focus", "Focus"),
    ("darks", "Darks"),
    ("stack", "Stack"),
    ("park", "Park"),
]
_POSITION = {key: index for index, (key, _) in enumerate(STEPS)}
_STAGE_STEP = {"AutoGoto": "slew", "AutoFocus": "focus", "DarkLibrary": "darks", "Stack": "stack"}
FILTERS = {0: "Dark", 1: "IR cut", 2: "LP"}


def event_map(reply) -> dict:
    """get_event_state's events by name. ALP answers this from its own cache."""
    events = pydash.get(reply, "Value.result")
    return events if isinstance(events, dict) else {}


def _event_state(events, name):
    return pydash.get(events, f"{name}.state")


def _waiting_detail(status):
    if not status.get("safe"):
        return "Waiting for the roof to read safe"
    sun = status.get("sun_deg")
    if sun is None:
        return "Waiting for dark"
    return f"Waiting for dark: sun {sun:.0f}\N{DEGREE SIGN}, limit {status.get('sun_max_deg')}\N{DEGREE SIGN}"


def _current_step(status, events):
    """Which step the scope is on, or None if it is not working through them."""
    phase = status.get("phase")
    if phase == "parking":
        return "park"
    if phase == "opening":
        return "open"
    # A stopped scheduler can still report a stage after the arm has closed, so
    # outside a session a closed arm wins. During one it can't: the firmware reports
    # closed from the startup sequence until the first goto.
    if status.get("parked") and phase != "imaging":
        return "ready" if not (status.get("safe") and status.get("dark")) else None
    if _event_state(events, "DarkLibrary") == "working":
        return "darks"
    if status.get("view_state") == "working" and status.get("view_stage") in _STAGE_STEP:
        return _STAGE_STEP[status["view_stage"]]
    if status.get("parked"):
        return "ready" if not (status.get("safe") and status.get("dark")) else None
    if status.get("sched_state") == "working":
        # Between steps, such as moving to the next target.
        return "slew"
    return None


def _active_detail(step, status, events):
    target = status.get("target") or "target"
    if step == "ready":
        return _waiting_detail(status)
    if step == "open":
        return "Opening the arm"
    if step == "slew":
        solving = _event_state(events, "PlateSolve") in ("start", "working", "solving")
        return f"Plate solving {target}" if solving else f"Moving to {target}"
    if step == "focus":
        return f"Focusing on {target}"
    if step == "darks":
        percent = pydash.get(events, "DarkLibrary.percent")
        return "Taking dark frames" + (f", {percent:.0f}%" if percent else "")
    if step == "stack":
        left = pydash.get(events, "scheduler.cur_scheduler_item.item_remaining_time_s")
        return f"Stacking {target}" + (f", {_duration(left)} left" if left else "")
    return "Closing the arm"


def _finished_row(key, events):
    """How a step behind the current one went."""
    if key == "focus" and _event_state(events, "AutoFocus") == "fail":
        return "warn"
    if key == "darks" and not _event_state(events, "DarkLibrary"):
        return "skipped"
    return "done"


# The scope keeps an event's last state until the next attempt replaces it, so a
# failure may be older than the current step. The wording says "last" for that reason.
_FAILURES = [
    ("AutoGoto", "goto"),
    ("PlateSolve", "plate solve"),
    ("AutoFocus", "focus"),
    ("DarkLibrary", "dark frames"),
]


def tracker_notes(events) -> list:
    """Short lines under the tracker: anything that failed, then quiet details."""
    notes = []
    for event, label in _FAILURES:
        if _event_state(events, event) == "fail":
            error = pydash.get(events, f"{event}.error")
            text = f"Last {label} failed" + (f": {error}" if error else "")
            notes.append({"text": text, "problem": True})
    position = pydash.get(events, "FocuserMove.position")
    if position is not None:
        notes.append({"text": f"Focus position {position}", "problem": False})
    return notes


def tracker_steps(status, events) -> dict:
    """One target's journey through STEPS, for the tracker at the top of the page."""
    events = events or {}
    finished_night = status.get("completed") and status.get("parked")
    current = None if finished_night else _current_step(status, events)
    position = _POSITION.get(current)
    rows = []
    for key, label in STEPS:
        index = _POSITION[key]
        if finished_night:
            state = _finished_row(key, events)
        elif position is None:
            state = "pending"
            if key in ("ready", "open") and not status.get("parked"):
                state = "done"
            elif key == "ready" and status.get("safe") and status.get("dark"):
                state = "done"
        elif current == "park" and index < position:
            # A stop can come at any step, so don't claim the ones it never reached.
            state = "done" if key in ("ready", "open") else "pending"
        elif index < position:
            state = _finished_row(key, events)
        elif index == position:
            state = "blocked" if key == "ready" and not status.get("safe") else "active"
        else:
            state = "pending"
        rows.append({"key": key, "label": label, "state": state})

    if status.get("connected") is False:
        detail = "Scope offline"
    elif finished_night:
        detail = "Schedule finished and parked"
    elif current:
        detail = _active_detail(current, status, events)
    elif status.get("held"):
        detail = "Stopped by hand. Held until Start or the next night"
    elif status.get("parked"):
        detail = "Parked, ready to start"
    else:
        detail = "Arm open, not imaging"

    if current and status.get("alarm"):
        rows[position]["state"] = "failed"
    elif current == "slew" and _event_state(events, "AutoGoto") == "fail":
        rows[position]["state"] = "failed"
        detail = "Goto failed"

    return {"steps": rows, "detail": detail, "notes": tracker_notes(events)}


def filter_name(events):
    return FILTERS.get(pydash.get(events or {}, "WheelMove.position"))


class ImageStore:
    """The newest stack image for one scope, as JPEGs, and when to fetch another.

    The imager only keeps the raw frame it last received. It asks the scope for
    one when a Stack event arrives after the Live page has been opened, and from
    then on keeps doing so whether anyone is looking or not. Here the request
    comes from the browser polling, so nothing is downloaded with no viewer.
    """

    def __init__(self, clock=time.time):
        self._lock = threading.Lock()
        self._clock = clock
        self._image = None
        self._rendered_frame = None
        self._requested_stacked = None
        self._requested_at = 0
        self._received_at_request = None
        self._pending = None

    def _maybe_request(self, imager, view):
        if not is_stacking(view) or not imager.comm.is_connected():
            return
        stacked = view["Stack"].get("stacked_frame") or 0
        if stacked < 1:
            return
        received = imager.comm.received_frame()
        changed = stacked != self._requested_stacked
        unanswered = received == self._received_at_request
        now = self._clock()
        if (changed or unanswered) and now - self._requested_at >= MIN_FETCH_INTERVAL_S:
            imager.request_stacked_image()
            self._requested_stacked = stacked
            self._requested_at = now
            self._received_at_request = received
            self._pending = {"target": view.get("target_name"), "stacked": stacked}

    def _render(self, imager, view):
        frame = imager.comm.received_frame()
        if frame == self._rendered_frame:
            return
        # Stretch outside the imager's lock: the receive thread needs it.
        raw, _, _ = imager.comm.get_unprocessed_image()
        if raw is None:
            return
        image = raw
        if not imager.comm.is_streaming():
            for processor in imager.comm.StarProcessors:
                image = processor.process(image)
        image = np.clip(image, 0, 255).astype(np.uint8)
        height, width = image.shape[:2]
        preview = image
        if width > PREVIEW_WIDTH:
            size = (PREVIEW_WIDTH, round(height * PREVIEW_WIDTH / width))
            preview = cv2.resize(image, size, interpolation=cv2.INTER_AREA)

        received_at = getattr(imager.comm, "received_at", lambda: None)()
        now = self._clock()
        # A label is only good for a reply to its own request, and not an image that
        # arrived before it was made.
        fresh = (
            self._pending
            and now - self._requested_at <= PENDING_MAX_AGE_S
            and (received_at is None or received_at >= self._requested_at)
        )
        if fresh:
            made = self._pending
        elif is_stacking(view):
            made = {"target": view.get("target_name"), "stacked": view["Stack"].get("stacked_frame")}
        else:
            # Parked or idle: the view's target is whatever it last had, which may not
            # be this image's. No name is better than a wrong one.
            made = {"target": None, "stacked": None}
        self._image = {
            "frame": frame,
            "target": made["target"],
            "stacked": made["stacked"],
            # When it reached us, not when a browser next happened to look.
            "captured_at": received_at or now,
            "snr": _snr(raw),
            "preview": _jpeg(preview, PREVIEW_QUALITY),
            "full": _jpeg(image, FULL_QUALITY),
        }
        self._rendered_frame = frame
        self._pending = None

    def pending(self) -> bool:
        """True just after a frame was asked for and before it has arrived."""
        with self._lock:
            return self._pending is not None and self._clock() - self._requested_at <= PENDING_QUICK_S

    def latest(self, imager, fetch=False, view=None):
        """Return the newest image; with fetch, also ask the scope for a newer one.

        ``view`` is the scope's current view when the caller has a fresher one than
        the device's, which only changes when something asks for get_view_state.
        """
        with self._lock:
            if imager is not None:
                view = view or imager.device.view_state
                if fetch:
                    self._maybe_request(imager, view)
                self._render(imager, view)
            return self._image


def _snr(raw):
    from device.analysis.snr_analysis import SNRAnalysis

    value = SNRAnalysis().analyze(raw)
    # Reads exactly 0 on real framed-mosaic stacks whose stars are plainly visible,
    # so 0 is not a usable measurement. Show nothing rather than "SNR 0".
    if isinstance(value, (int, float, np.floating)) and np.isfinite(value) and value > 0:
        return round(float(value), 1)
    return None


def _jpeg(image, quality) -> bytes:
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("JPEG encoding failed")
    return encoded.tobytes()


def download_name(image) -> str:
    target = re.sub(r"[^A-Za-z0-9_-]+", "_", image.get("target") or "seestar").strip("_")
    taken = datetime.fromtimestamp(image["captured_at"]).strftime("%Y-%m-%d_%H%M")
    frames = f"_{image['stacked']}frames" if image.get("stacked") else ""
    return f"{target or 'seestar'}_{taken}{frames}.jpg"


_stores = {}
_stores_lock = threading.Lock()


def _store(telescope_id) -> ImageStore:
    with _stores_lock:
        return _stores.setdefault(telescope_id, ImageStore())


def _imager(telescope_id):
    from device import telescope

    try:
        return telescope.get_seestar_imager(telescope_id)
    except (KeyError, IndexError):
        return None


def _image_meta(image) -> dict:
    if not image:
        return {"frame": None}
    return {
        "frame": image["frame"],
        "target": image["target"],
        "stacked": image["stacked"],
        "captured_at": image["captured_at"],
        "captured_clock": site_clock(image["captured_at"]),
        "snr": image["snr"],
    }


def cached_view(telescope_id, status):
    """The view as the automation loop last read it, or None if it can't say.

    Tonight doesn't poll get_view_state, so the device's copy goes stale. The loop
    reads the scope anyway, so using its reading adds no messages.
    """
    try:
        same_scope = roof_config.load().device_num == telescope_id
    except Exception:
        return None
    if not same_scope or status.get("loop_stale") or "stacked" not in status:
        return None
    return {
        "state": status.get("view_state"),
        "stage": status.get("view_stage"),
        "target_name": status.get("view_target"),
        "Stack": {"stacked_frame": status.get("stacked"), "dropped_frame": status.get("dropped")},
    }


class PierStore:
    """The latest image PFR Sentinel published. Fetched only while a browser polls.

    Keeps the last good image through a failed fetch, and records when it was
    captured so an old view of the roof is never shown as if it were current.
    """

    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._image = None
        self._checked_at = None
        self._online = False

    def latest(self, image_url: str, status_url: str) -> dict | None:
        with self._lock:
            now = self._clock()
            if self._checked_at is None or now - self._checked_at >= PIER_FETCH_INTERVAL_S:
                self._checked_at = now
                self._online = self._fetch(image_url, status_url, now)
            return self._image

    def peek(self) -> dict | None:
        """The image already held, without fetching."""
        with self._lock:
            return self._image

    @property
    def online(self) -> bool:
        return self._online

    def _fetch(self, image_url, status_url, now) -> bool:
        try:
            reply = requests.get(image_url, timeout=PIER_TIMEOUT_S)
            reply.raise_for_status()
            age = requests.get(status_url, timeout=PIER_TIMEOUT_S).json().get("image_age_seconds")
            tag = reply.headers.get("ETag") or str(hash(reply.content))
            if self._image and self._image["tag"] == tag:
                return True
            decoded = cv2.imdecode(np.frombuffer(reply.content, np.uint8), cv2.IMREAD_COLOR)
            if decoded is None:
                raise ValueError("not an image")
            height, width = decoded.shape[:2]
            if width > PIER_PREVIEW_WIDTH:
                size = (PIER_PREVIEW_WIDTH, round(height * PIER_PREVIEW_WIDTH / width))
                decoded = cv2.resize(decoded, size, interpolation=cv2.INTER_AREA)
            self._image = {
                "tag": tag,
                "preview": _jpeg(decoded, PREVIEW_QUALITY),
                "full": reply.content,
                "captured_at": now - float(age or 0),
            }
            return True
        except Exception:
            return False

    def meta(self, image) -> dict:
        if not image:
            return {"available": False, "online": self._online}
        return {
            "available": True,
            "online": self._online,
            "frame": image["tag"],
            "captured_at": image["captured_at"],
            "stale": self._clock() - image["captured_at"] > PIER_STALE_S,
        }


_pier_store = PierStore()


def _pier_image():
    settings = roof_config.load()
    return _pier_store.latest(settings.pier_image_url, settings.pier_status_url)


def pier_meta(image) -> dict:
    meta = _pier_store.meta(image)
    if image:
        meta["captured_clock"] = site_clock(image["captured_at"])
    return meta


class DashboardLive:
    """Roof, sky, session and plan. Refreshed often."""

    @staticmethod
    def on_get(req, resp, telescope_id=1):
        front = _front()
        status = get_automation().status()
        view = pydash.get(front.method_sync("get_view_state", telescope_id), "View")
        schedule = pydash.get(
            front.do_action_device("get_schedule", telescope_id, {}), "Value"
        )
        session = session_summary(view, schedule)
        events = event_map(front.do_action_device("get_event_state", telescope_id, {}))
        session["filter"] = filter_name(events)
        details = list_details(status, session)
        session["goal"] = details.get("goal")
        front.render_fragment(
            req,
            resp,
            "partials/dashboard_live.html",
            status=status,
            session=session,
            plan=details["rows"] if "rows" in details else plan_rows(status, session),
            plan_skipped=details.get("skipped") or [],
            plan_error=details.get("error"),
        )


class DashboardTracker:
    """The workflow tracker. Polled often, so it never sends a message to the scope:
    the automation loop caches the stage and ALP caches the events."""

    @staticmethod
    def on_get(req, resp, telescope_id=1):
        front = _front()
        events = event_map(front.do_action_device("get_event_state", telescope_id, {}))
        front.render_fragment(
            req,
            resp,
            "partials/dashboard_tracker.html",
            tracker=tracker_steps(get_automation().status(), events),
        )


class DashboardDevice:
    """The hardware readings. These cost several calls to the scope and change slowly."""

    @staticmethod
    def on_get(req, resp, telescope_id=1):
        front = _front()
        stats = front.get_device_state(telescope_id)
        rows = device_rows(stats)
        if stats and not rows:
            # The scope is online but didn't answer, which can happen while it
            # images. Keep the last readings rather than claim it is offline.
            resp.status = falcon.HTTP_204
            return
        front.render_fragment(req, resp, "partials/dashboard_device.html", rows=rows)


class DashboardFocus:
    """The autofocus curve while a run is showing, and nothing otherwise.

    An empty reply is what puts the image back, so this one is meant to swap in
    empty. Polled often, so it only reads what roof.focus has already kept.
    """

    @staticmethod
    def on_get(req, resp, telescope_id=1):
        run = focus.focus_log(telescope_id).current()
        _front().render_fragment(
            req,
            resp,
            "partials/dashboard_focus.html",
            chart=focus.chart(run) if run else None,
        )


class DashboardImageMeta:
    """What the browser polls. A poll is also what prompts a fresh fetch."""

    @staticmethod
    def on_get(req, resp, telescope_id=1):
        view = cached_view(telescope_id, get_automation().status())
        store = _store(telescope_id)
        image = store.latest(_imager(telescope_id), fetch=True, view=view)
        meta = _image_meta(image)
        if store.pending():
            meta["pending"] = True
        resp.content_type = "application/json"
        resp.set_header("Cache-Control", "no-store")
        resp.text = json.dumps(meta)


class DashboardImage:
    @staticmethod
    def on_get(req, resp, telescope_id=1):
        image = _store(telescope_id).latest(_imager(telescope_id))
        if not image:
            raise falcon.HTTPNotFound(description="no image yet")
        resp.content_type = "image/jpeg"
        resp.set_header("Cache-Control", "no-store")
        if req.get_param_as_bool("full"):
            resp.data = image["full"]
            resp.set_header(
                "Content-Disposition", f'attachment; filename="{download_name(image)}"'
            )
        else:
            resp.data = image["preview"]


class DashboardPierMeta:
    """What the browser polls. A poll is also what prompts a fresh fetch."""

    @staticmethod
    def on_get(req, resp, telescope_id=1):
        image = _pier_image()
        resp.content_type = "application/json"
        resp.set_header("Cache-Control", "no-store")
        resp.text = json.dumps(pier_meta(image))


class DashboardPierImage:
    @staticmethod
    def on_get(req, resp, telescope_id=1):
        image = _pier_image()
        if not image:
            raise falcon.HTTPNotFound(description="no pier image")
        resp.content_type = "image/jpeg"
        resp.set_header("Cache-Control", "no-store")
        resp.data = image["full"] if req.get_param_as_bool("full") else image["preview"]


# The page itself is in roof/tonight.py.
PAGES = {
    "dashboard/live": DashboardLive,
    "dashboard/tracker": DashboardTracker,
    "dashboard/device": DashboardDevice,
    "dashboard/focus": DashboardFocus,
    "dashboard/image.json": DashboardImageMeta,
    "dashboard/image.jpg": DashboardImage,
    "dashboard/pier.json": DashboardPierMeta,
    "dashboard/pier.jpg": DashboardPierImage,
}
