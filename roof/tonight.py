"""The Tonight screen at /dashboard: one state, one sentence, at most one action.

tonight_state() is pure: automation status and cached events in, what the page says
out. Every word it shows is something the data supports. When the data can't support
the spec's sentence it says less: "Parking" appears only while the loop is parking.

/tonight/state never sends a message to the scope. It reads the automation loop's
last snapshot, ALP's cached events and files on this computer.
"""

import threading
import time
from datetime import datetime, timedelta

import falcon
import pydash

from roof import config as roof_config
from roof import dashboard, focus, night_web, planner, schedule_tools
from roof.sessions import base_name
from roof.sun import night_key

# Below this the sky counts as twilight on Tonight: Day shows the plan, not the pier.
DAY_SUN_DEG = -6.0
# The page stops offering Stop once pressed, until the loop reports the result. A stop
# holds the loop's tick for as long as the scope's threads take to wind down.
STOP_PENDING_S = 120
PLAN_CACHE_S = 60
# Share of the Night Line a label needs to reach 64px on a phone, tablet and desktop.
LABEL_TIERS = ((18.0, 1), (9.0, 2), (5.3, 3))
MAX_PHONE_LABELS = 3
ROOF_NOTE_PERIODS = 2

# Hero: what fills the slot. "plan" is the Night Line and rows, with no large image.
HEROES = {
    "idle": "plan",
    "waiting_dark": "pier",
    "moving": "pier",
    "autofocus": "focus",
    "darks": "stack",
    "stacking": "stack",
    "held": "stack",
    "finished": "stack",
    "roof_unsafe": "pier",
    "now_parked": "pier",
    "offline": "stack",
    "parking": "pier",
    "arm_open": "pier",
    "elsewhere": "stack",
}
DIMMED = {"darks", "held", "offline"}
# Where the night's summary may show once dark is over. While something is moving or
# the roof needs attention, that comes first.
MORNING = {"idle", "held", "offline"}
STOPPABLE = {"moving", "autofocus", "darks", "stacking", "roof_unsafe", "arm_open", "elsewhere"}


def _plain_clock(timestamp) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%H:%M")


def _degrees(value) -> str:
    rounded = round(value)
    return f"\N{MINUS SIGN}{-rounded}\N{DEGREE SIGN}" if rounded < 0 else f"{rounded}\N{DEGREE SIGN}"


def _hours(value) -> str:
    return f"{value:.1f}".removesuffix(".0") + " h"


def _session_running(status) -> bool:
    return status.get("phase") in ("opening", "imaging") or status.get("sched_state") == "working"


def classify(status, events=None, window=None, now=None) -> str:
    """Which state the data says the observatory is in. Order is the precedence."""
    events = events or {}
    # A stale snapshot says nothing current, so nothing else in it can be believed.
    if status.get("loop_stale"):
        return "offline"
    connected = status.get("connected")
    parked = status.get("parked")
    settled = parked is True and not _session_running(status) and status.get("phase") != "parking"
    if not status.get("safe") and not status.get("override_roof"):
        if not connected or not settled:
            return "roof_unsafe"
        # The roof reads unsafe all day while closed, which isn't news.
        if status.get("dark") and not status.get("held") and not status.get("completed"):
            return "now_parked"
    if not connected:
        return "offline"
    if status.get("phase") == "parking":
        return "parking"
    if settled and status.get("held"):
        return "held"
    if settled and status.get("completed"):
        return "finished"

    step = dashboard._current_step(status, events)
    if step in ("open", "slew"):
        return "moving"
    if step == "focus":
        return "autofocus"
    if step == "darks":
        return "darks"
    if step == "stack":
        return "stacking"
    if step == "park":
        return "parking"
    if status.get("foreign"):
        return "elsewhere"
    if parked is not True:
        return "arm_open"
    return _waiting_or_idle(status, window, now)


def _waiting_or_idle(status, window, now) -> str:
    sun = status.get("sun_deg")
    if status.get("dark") or sun is None or sun > DAY_SUN_DEG:
        return "idle"
    if not status.get("enabled") or not status.get("schedule"):
        return "idle"
    # Morning twilight isn't waiting for anything.
    dark_end = (window or {}).get("dark_end")
    if dark_end is not None and now is not None and now >= dark_end:
        return "idle"
    return "waiting_dark"


def _log_time(status, text):
    """When the newest log line containing ``text`` was written, as a timestamp."""
    for line in reversed(status.get("log") or []):
        if text in line:
            try:
                return datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                return None
    return None


def _idle_sentence(status, plan) -> str:
    if not status.get("enabled"):
        return "Automation off, nothing will start"
    if not status.get("schedule"):
        return "No schedule selected"
    if plan.get("error"):
        return plan["error"]
    if status.get("sun_deg") is None:
        return "No site location set, so it never counts as dark"
    upcoming = plan.get("next")
    note = str(status.get("note") or "")
    if status.get("dark") and note and "next target" not in note:
        text = note.removeprefix("waiting: ")
        return text[0].upper() + text[1:]
    condition = "" if status.get("safe") else " if the roof reads safe"
    if upcoming:
        return f"Starts {upcoming['clock']} with {upcoming['name']}{condition}"
    if plan.get("first_name"):
        return f"Starts after dark with {plan['first_name']}{condition}"
    if status.get("dark"):
        return "Parked, ready to start"
    return "Nothing to image for the rest of this night"


def _held_sentence(window, now) -> str:
    dark_end = (window or {}).get("dark_end")
    if dark_end is None or now is None or now >= dark_end:
        return "Held until the next night"
    left = (dark_end - max(now, window.get("dark_start") or now)) / 3600
    return f"Held. {_hours(round(left, 1))} of dark left"


def _roof_unsafe_sentence(status) -> str:
    if not status.get("connected"):
        return "Roof unsafe. Scope offline, cannot park"
    if status.get("phase") == "parking":
        return "Roof unsafe. Parking"
    if not status.get("enabled"):
        return "Roof unsafe. Automation off, will not park"
    if status.get("parked") is None:
        return "Roof unsafe. Arm position unknown"
    return "Roof unsafe. Arm still open"


def _offline_sentence(status, clock, now) -> str:
    if status.get("loop_stale"):
        age = status.get("loop_age_s")
        if age is None or now is None:
            return "Automation has not reported yet"
        return f"Automation stopped reporting, last report {clock(now - age)}"
    seen = status.get("last_connected_at")
    return f"Scope offline, last seen {clock(seen)}" if seen else "Scope offline"


def _moving_sentence(status, events) -> str:
    if status.get("phase") == "opening":
        return "Opening the arm"
    target = status.get("target")
    if dashboard._event_state(events, "PlateSolve") in ("start", "working", "solving"):
        return f"Plate solving {target}" if target else "Plate solving"
    return f"Moving to {target}" if target else "Moving to the next target"


def _focus_sentence(status, events) -> str:
    position = pydash.get(events, "FocuserMove.position")
    if position is not None:
        return f"Focusing, position {position}"
    target = status.get("target")
    return f"Focusing on {target}" if target else "Focusing"


def _darks_percent(events):
    percent = pydash.get(events, "DarkLibrary.percent")
    return round(percent) if isinstance(percent, (int, float)) else None


def _sentence(key, status, events, plan, clock, now) -> str:
    window = plan.get("window")
    if key == "idle":
        return _idle_sentence(status, plan)
    if key == "waiting_dark":
        return f"Waiting for dark, sun {_degrees(status['sun_deg'])}"
    if key == "moving":
        return _moving_sentence(status, events)
    if key == "autofocus":
        return _focus_sentence(status, events)
    if key == "darks":
        percent = _darks_percent(events)
        return f"Taking darks, {percent}%" if percent else "Taking darks"
    if key == "stacking":
        target = status.get("target") or status.get("view_target")
        return f"Stacking {target}" if target else "Stacking"
    if key == "held":
        return _held_sentence(window, now)
    if key == "finished":
        earned = plan.get("earned_hours")
        return f"Finished. {_hours(round(earned, 1))} earned" if earned else "Finished"
    if key == "roof_unsafe":
        return _roof_unsafe_sentence(status)
    if key == "now_parked":
        # Says the scope is safe, because the roof still isn't.
        parked_at = _log_time(status, "parked: firmware reports mount closed")
        return f"Parked {clock(parked_at)}, scope safe" if parked_at else "Parked, scope safe"
    if key == "offline":
        return _offline_sentence(status, clock, now)
    if key == "parking":
        return "Parking"
    if key == "elsewhere":
        return "A session started elsewhere is running"
    return "Arm position unknown" if status.get("parked") is None else "Arm open, not imaging"


def _metrics(key, stack) -> list:
    if key != "stacking":
        return []
    stack = stack or {}
    metrics = [
        {"label": "Earned", "value": stack.get("earned") or "\N{EM DASH}", "unit": "", "hint": ""},
        {
            "label": "Kept",
            "value": f"{stack['kept_percent']}%" if stack.get("kept_percent") is not None else "\N{EM DASH}",
            "unit": "",
            "hint": stack.get("frames") or "",
        },
    ]
    goal = stack.get("goal")
    if goal:
        metrics.append(
            {
                "label": "Goal",
                "value": f"{goal['have']:.1f}",
                "unit": f"of {goal['goal']:g} h",
                "hint": f"plus {goal['partial']:.1f} h not counted yet" if goal["partial"] >= 0.05 else "",
            }
        )
    return metrics


def _bands(key, sentence, status) -> list:
    """Active danger. Each stays on screen for as long as it's true."""
    bands = []
    if status.get("loop_stale"):
        bands.append(
            {
                "headline": "The automation loop has stopped reporting",
                "line": "Nothing will park the scope automatically until this clears.",
            }
        )
    if status.get("alarm"):
        bands.append({"headline": "Automation alarm", "line": str(status["alarm"])})
    if status.get("override_roof"):
        bands.append(
            {
                "headline": "Manual start is ignoring the roof",
                "line": "It will not park for the roof until the roof next reads safe.",
            }
        )
    if key == "roof_unsafe":
        detail = status.get("safety_detail")
        bands.append({"headline": sentence, "line": f"Roof sensor: {detail}" if detail else ""})
    return bands


def _notices(key, status, events, here) -> list:
    notices = []
    if not here:
        name = status.get("scope_name") or "the automation's scope"
        notices.append(
            {"tone": "attention", "text": f"Status here is for {name}, the scope roof automation runs. Stop is on its page."}
        )
    if status.get("dry_run"):
        notices.append({"tone": "attention", "text": "Dry run. The automation only pretends to move the scope."})
    if key == "now_parked":
        detail = status.get("safety_detail")
        notices.append({"tone": "attention", "text": f"Roof reads unsafe: {detail}" if detail else "Roof reads unsafe"})
    if status.get("foreign") and key not in ("elsewhere", "offline"):
        notices.append({"tone": "neutral", "text": "This session started elsewhere, and automation is leaving it alone."})
    if (
        not status.get("loop_stale")
        and not status.get("enabled")
        and status.get("connected")
        and (status.get("parked") is not True or _session_running(status))
    ):
        notices.append({"tone": "attention", "text": "Automation off. It will not park for the roof or for dawn."})
    if status.get("resume_item") and key not in ("stacking", "moving", "autofocus", "darks"):
        notices.append({"tone": "neutral", "text": f"Will resume at item {status['resume_item']} tonight."})
    for note in dashboard.tracker_notes(events):
        if note["problem"]:
            notices.append({"tone": "attention", "text": note["text"]})
    return notices


def _action(key, status, here, stop_requested_at, clock, now):
    if (
        key in STOPPABLE
        and here
        and status.get("connected")
        and not status.get("loop_stale")
        and status.get("phase") != "parking"
    ):
        pending = (
            stop_requested_at is not None
            and now is not None
            and 0 <= now - stop_requested_at < STOP_PENDING_S
        )
        return {
            "kind": "stop",
            "stopping": pending,
            "since": clock(stop_requested_at) if pending else None,
        }
    if key == "held":
        return {"kind": "link", "label": "Open Automation", "route": "roof"}
    if key == "idle":
        if not status.get("schedule"):
            return {"kind": "link", "label": "Choose a schedule", "route": "roof"}
        route = "targets" if status.get("uses_target_list") else "roof"
        return {"kind": "link", "label": "Edit plan", "route": route}
    return None


def _summary_hours(entry) -> str:
    """A target's hours. A short run shows in minutes, so it doesn't read as nothing."""
    if entry["hours"] >= 0.05 or not entry.get("stacked"):
        return _hours(round(entry["hours"], 1))
    return _earned_text(entry["hours"] * 3600) or _hours(0)


def _summary_line(entry) -> str:
    """Kept, the goal, and how much of the hours shown the goal doesn't count yet."""
    parts = []
    if entry.get("kept_percent") is not None:
        parts.append(f"Kept {entry['kept_percent']}%")
    goal = entry.get("goal")
    if goal:
        parts.append(f"{goal['have_hours']:.1f} of {goal['goal_hours']:g} h toward goal")
    if entry.get("hours_not_counted", 0) >= 0.05:
        parts.append(f"{entry['hours_not_counted']:.1f} h of this not counted yet")
    return ". ".join(parts)


def _morning(key, plan, now):
    """The summary of the night just worked. Every figure is from the session log."""
    if key != "finished":
        over = plan.get("summary_dark_end")
        if key not in MORNING or over is None or now is None or now < over:
            return None
    error = plan.get("summary_error")
    entries = (plan.get("summary") or {}).get("targets") or []
    if not entries and not error:
        return None
    sunrise = plan.get("summary_sunrise")
    rows = [{"name": e["name"], "hours": _summary_hours(e), "line": _summary_line(e)} for e in entries]
    total = None
    # The Finished sentence already gives the total.
    if len(rows) > 1 and key != "finished":
        total = _summary_hours({"hours": plan["summary"]["total_hours"], "stacked": 1})
    return {
        "title": "Last night" if sunrise is not None and now is not None and now >= sunrise else "This night",
        "rows": rows,
        "total": total,
        "error": error,
    }


def tonight_state(
    status,
    events=None,
    stack=None,
    plan=None,
    now=None,
    clock=_plain_clock,
    stop_requested_at=None,
    here=True,
) -> dict:
    """Everything the page says for one snapshot. ``here`` is False when the page's
    scope isn't the one the automation runs, because Stop would park the other one."""
    events = events or {}
    plan = plan or {}
    key = classify(status, events, plan.get("window"), now)
    sentence = _sentence(key, status, events, plan, clock, now)
    action = _action(key, status, here, stop_requested_at, clock, now)
    tone = "neutral"
    if key == "offline":
        tone = "danger" if status.get("loop_stale") else "attention"
    morning = _morning(key, plan, now)
    return {
        "key": key,
        "hero": HEROES[key],
        "dim": key in DIMMED,
        "sentence": sentence,
        # The band already says it. No fact twice.
        "sentence_in_band": key == "roof_unsafe",
        "tone": tone,
        "metrics": _metrics(key, stack),
        "action": action,
        "stop": (action or {}).get("kind") == "stop" and not action["stopping"],
        "bands": _bands(key, sentence, status),
        "notices": _notices(key, status, events, here),
        "bar": _darks_percent(events) if key == "darks" else None,
        "bar_shown": key == "darks",
        "steps": key == "moving",
        "morning": morning,
        # The image on show is what's left of a session, so its caption must say so.
        "last_image": key in DIMMED or key == "finished" or bool(morning),
        # The caption needn't repeat the sentence's target, if it's the image's too.
        "stack_target": (status.get("target") or status.get("view_target") or "") if key == "stacking" else "",
    }


def roof_status(status) -> list:
    """The top bar's status, the same facts the legacy badge gives."""
    if status.get("loop_stale"):
        lines = [{"tone": "danger", "text": "Loop stopped"}]
    elif status.get("alarm"):
        lines = [{"tone": "danger", "text": "Roof alarm"}]
    elif status.get("safe"):
        lines = [{"tone": "ok", "text": "Roof safe"}]
    else:
        lines = [{"tone": "danger", "text": "Roof unsafe"}]
    if not status.get("loop_stale") and not status.get("connected"):
        lines.append({"tone": "attention", "text": "Scope offline"})
    if status.get("dry_run"):
        lines.append({"tone": "attention", "text": "Dry run"})
    return lines


def _merge_spans(spans) -> list:
    """Spans that touch read as one. Restarts otherwise split a gap into several."""
    merged = []
    for span in sorted(spans, key=lambda s: s["start"]):
        if merged and span["start"] <= merged[-1]["end"]:
            merged[-1] = {**merged[-1], "end": max(merged[-1]["end"], span["end"])}
        else:
            merged.append(dict(span))
    return merged


def _roof_words(closed, unknown, clock, night_start=None) -> list:
    """The roof's record in words, one string per fact."""
    words = []
    for period in closed:
        what = "Roof closed" if period["reason"] == "unsafe" else "Roof sensor not answering, counted as closed,"
        if period["open"]:
            words.append(f"{what} since {clock(period['start'])}")
        else:
            words.append(f"{what} {clock(period['start'])} to {clock(period['end'])}")
    for span in _merge_spans(unknown):
        # Not a fault: nothing was recording, and the top bar says what the roof is now.
        if span["start"] == night_start:
            words.append(f"No roof record before {clock(span['end'])}")
        else:
            words.append(f"No roof record {clock(span['start'])} to {clock(span['end'])}")
    return words


def _roof_layers(roof, sunset, sunrise, now, percent, clock) -> dict:
    """What to draw of the roof's record: only what falls between sunset and sunrise,
    with its times clipped to match, and with an open period running to now."""

    def clip(start, end):
        start, end = max(start, sunset), min(end, sunrise)
        return (start, end) if end > start else None

    closed, unknown = [], []
    for period in roof.get("closed") or ():
        end = period["end"] if period["end"] is not None else (now if now is not None else period["start"])
        span = clip(period["start"], end)
        if span:
            closed.append({**period, "start": span[0], "end": span[1]})
    for period in roof.get("unknown") or ():
        span = clip(period["start"], period["end"])
        if span:
            unknown.append({"start": span[0], "end": span[1]})
    words = _roof_words(closed, unknown, clock, sunset)
    note = words
    # Few enough to read on a phone. The label keeps every one.
    if len(closed) > ROOF_NOTE_PERIODS:
        hours = sum(p["end"] - p["start"] for p in closed) / 3600
        note = [f"Roof closed {len(closed)} times, {_hours(round(hours, 1))} in all"] + words[len(closed) :]
    drawn = lambda spans: [{"start": percent(p["start"]), "end": percent(p["end"])} for p in spans]  # noqa: E731
    return {"closed": drawn(closed), "unknown": drawn(unknown), "words": words, "note": ". ".join(note)}


def night_line(window, blocks=(), now=None, clock=_plain_clock, roof=None) -> dict:
    """The Night Line in percentages of sunset to sunrise. ``roof`` is what
    NightHistory.roof_record gives. Left out, the roof isn't mentioned at all."""
    window = window or {}
    sunset, sunrise = window.get("sunset"), window.get("sunrise")
    if sunset is None or sunrise is None or sunrise <= sunset:
        return {"available": False, "label": "No night times, because no site location is set or the sun does not set."}

    def percent(when):
        return round(max(0.0, min(100.0, (when - sunset) * 100 / (sunrise - sunset))), 2)

    words = [f"Night from sunset {clock(sunset)} to sunrise {clock(sunrise)}."]
    dark = None
    if window.get("dark_start") is not None:
        dark = {"start": percent(window["dark_start"]), "end": percent(window["dark_end"])}
        words.append(f"Dark {clock(window['dark_start'])} to {clock(window['dark_end'])}.")
    else:
        words.append("No full darkness.")

    drawn = []
    phone_labels = 0
    labelled_to = None
    for block in blocks:
        if block.get("start") is None or block.get("end") is None:
            continue
        start, end = percent(block["start"]), percent(block["end"])
        if end <= start:
            continue  # wholly outside this night, so there's nothing to draw
        state = block.get("progress") or "next"
        tier = next((t for share, t in LABEL_TIERS if end - start >= share), None)
        # Blocks can overlap, and two labels in the same place can't both be read.
        if tier and labelled_to is not None and start < labelled_to:
            tier = None
        if tier:
            labelled_to = end
        if tier == 1:
            phone_labels += 1
            if phone_labels > MAX_PHONE_LABELS:
                tier = 2
        drawn.append({"name": block["name"], "start": start, "end": end, "state": state, "tier": tier})
        word = {"done": "passed", "now": "now", "next": "next"}[state]
        words.append(f"{block['name']} {clock(block['start'])} to {clock(block['end'])}, {word}.")

    layers = _roof_layers(roof or {}, sunset, sunrise, now, percent, clock)
    words += [f"{text}." for text in layers["words"]]

    marker = None
    if now is not None and sunset <= now <= sunrise:
        marker = percent(now)
        words.append(f"It is {clock(now)}.")
    return {
        "available": True,
        "sunset": clock(sunset),
        "sunrise": clock(sunrise),
        "dark": dark,
        "blocks": drawn,
        "closed": layers["closed"],
        "unknown": layers["unknown"],
        "roof_note": layers["note"],
        "now": marker,
        "label": " ".join(words),
    }


def _earned_text(seconds):
    if seconds <= 0:
        return None
    return f"{seconds:.0f} s" if seconds < 60 else dashboard._duration(seconds)


def stack_summary(status, rows=(), goal=None) -> dict:
    """Earned and kept from what the loop last read of the stack."""
    stacked = status.get("stacked") or 0
    dropped = status.get("dropped") or 0
    exposure = status.get("exposure_s") or 0
    if not exposure:
        # Between exposures the firmware reports none. The session log kept the last.
        key = base_name(status.get("view_target") or "")
        for row in reversed(list(rows)):
            if row.get("target") == key and row.get("exposure_s"):
                exposure = row["exposure_s"]
                break
    total = stacked + dropped
    return {
        "stacked": stacked,
        "dropped": dropped,
        "exposure_s": exposure or None,
        "earned": _earned_text(stacked * exposure) if exposure else None,
        "kept_percent": 100 - round(dropped * 100 / total) if total else None,
        "frames": f"{stacked} of {total} frames" if total else "",
        "goal": goal,
    }


def details_rows(status, stack, events, clock) -> list:
    """Everything that's true but not worth the main view."""
    rows = []

    def add(label, value):
        if value not in (None, ""):
            rows.append({"label": label, "value": value})

    add("Target", status.get("target") or status.get("view_target"))
    scale = status.get("mosaic_scale")
    if scale is not None:
        add("Framing", f"Mosaic {scale:g}x" if scale > 1.0 else "Single frame")
    exposure = stack.get("exposure_s")
    gain = status.get("gain")
    settings = [f"{exposure:g} s" if exposure else "", f"gain {gain}" if gain is not None else ""]
    add("Exposure", ", ".join(s for s in settings if s))
    add("Filter", dashboard.filter_name(events))
    if stack.get("stacked") or stack.get("dropped"):
        add("Frames", f"{stack['stacked']} stacked, {stack['dropped']} dropped")
        add("Time on target", dashboard._duration(status.get("stack_lapse_s") or 0))
    left = pydash.get(events, "scheduler.cur_scheduler_item.item_remaining_time_s")
    if left:
        add("Time left", dashboard._duration(left))
    add("Focus position", pydash.get(events, "FocuserMove.position"))
    sun = status.get("sun_deg")
    limit = status.get("sun_max_deg")
    if sun is not None:
        add("Sun", _degrees(sun) + (f", limit {_degrees(limit)}" if limit is not None else ""))
    parked = status.get("parked")
    add("Arm", "Unknown" if parked is None else "Parked" if parked else "Open")
    add("Automation", "On" if status.get("enabled") else "Off")
    add("Plan", (status.get("schedule") or "").replace(".json", ""))
    add("Roof sensor", status.get("safety_detail"))
    margin = status.get("dew_margin_c")
    if margin is not None:
        heater = {True: "heater on", False: "heater off"}.get(status.get("heater_on"), "heater unknown")
        add("Dew margin", f"{margin:.1f} \N{DEGREE SIGN}C, {heater}")
    return rows


# ---- reading it all without asking the scope -----------------------------------------

_stop_requested_at = None
_plan_cache = {}
_plan_lock = threading.Lock()
_window_cache = {}


def note_stop_requested(when=None):
    global _stop_requested_at
    _stop_requested_at = when if when is not None else _now()


def _now():
    return time.time()


def _window(night, settings):
    if settings.lat == 0 and settings.lon == 0:
        return None
    key = (night, settings.lat, settings.lon, settings.sun_max_deg)
    if key not in _window_cache:
        if len(_window_cache) >= 4:
            _window_cache.clear()  # only tonight's and tomorrow's are ever asked for
        _window_cache[key] = planner.night_times(night, settings.lat, settings.lon, settings.sun_max_deg)
    return _window_cache[key]


def _showing_night(status, settings, now):
    """The night the page is about. After sunrise that's the coming one, unless this
    one still has something to report."""
    night = night_key(now, settings.lon)
    window = _window(night, settings)
    done = (window or {}).get("sunrise")
    if done is None or now < done or status.get("held") or status.get("completed"):
        return night, window, now
    following = (datetime.fromisoformat(night) + timedelta(days=1)).date().isoformat()
    # Planning "from" the next noon gives that whole night, as the planner would then.
    return following, _window(following, settings), planner.night_noon(following, settings.lon)


def _list_plan(status, settings, plan_from, now):
    targets = dashboard.target_queue().all()
    log = dashboard.session_log()
    altitude = lambda block, s, _when: dashboard._altitude_now(block, s, now)  # noqa: E731
    return dashboard.target_list_plan(status, {}, targets, log, settings, plan_from, altitude)


def _cached_list_plan(status, settings, night, plan_from, now):
    key = (night, status.get("schedule"), status.get("sched_state"), status.get("target_index"))
    with _plan_lock:
        cached = _plan_cache.get("list")
        if cached and cached["key"] == key and now - cached["at"] < PLAN_CACHE_S:
            return cached["plan"]
        plan = _list_plan(status, settings, plan_from, now)
        _plan_cache["list"] = {"key": key, "at": now, "plan": plan}
        return plan


def _add_summary(plan, settings, now, log):
    """The night now ending, even once the page has moved on to the coming one. It
    stops being that night at the same noon the automation's completed flag does."""
    night = night_key(now, settings.lon)
    window = _window(night, settings) or {}
    plan["summary_dark_end"] = window.get("dark_end")
    plan["summary_sunrise"] = window.get("sunrise")
    # Optional, as in night_web: a bad row mustn't lose the rest of the page.
    try:
        plan["summary"] = night_web.night_summary(night, log, dashboard.target_queue().all()) if log else {}
    except Exception as e:
        plan["summary"] = {}
        plan["summary_error"] = f"Could not read the session log: {e}"
    plan["earned_hours"] = plan["summary"].get("total_hours", 0)


def _roof_record(plan, settings, now) -> dict:
    """What's known of the roof on the night shown. With no record, none of it is."""
    history = getattr(dashboard.get_automation(), "_night_history", None)
    if history is not None:
        try:
            return history.roof_record(plan["night"], settings.lon, now)
        except Exception:
            pass
    sunset = (plan.get("window") or {}).get("sunset")
    return {"closed": [], "unknown": [{"start": sunset, "end": now}] if sunset is not None else []}


def gather_plan(status, settings, now, clock) -> dict:
    """The night's times and what's planned in them. The target list's plan is
    costly to work out and changes slowly, so it's kept for a minute."""
    night, window, plan_from = _showing_night(status, settings, now)
    plan = {"window": window, "night": night, "rows": [], "skipped": [], "blocks": []}
    log = dashboard.session_log()
    plan["log_rows"] = log.rows() if log else []
    _add_summary(plan, settings, now, log)
    if status.get("uses_target_list"):
        try:
            details = _cached_list_plan(status, settings, night, plan_from, now)
        except Exception as e:
            plan["error"] = f"Could not read the target list: {e}"
            return plan
        plan["rows"] = details["rows"]
        plan["skipped"] = details["skipped"]
        plan["blocks"] = details["rows"]
        ahead = [r for r in details["rows"] if r.get("start") and r["start"] > now]
        if ahead:
            plan["next"] = {"name": ahead[0]["name"], "clock": clock(ahead[0]["start"])}
    else:
        plan["rows"] = dashboard.plan_rows(status, {})
        names = [t.get("name") for t in status.get("targets") or []]
        plan["first_name"] = names[0] if names else None
    return plan


def _goal(status) -> dict | None:
    if not status.get("uses_target_list"):
        return None
    name = status.get("target") or status.get("view_target")
    if not name:
        return None
    try:
        key = base_name(name)
        log = dashboard.session_log()
        for target in dashboard.target_queue().all():
            if base_name(target.name) == key:
                progress = log.progress(target)
                return {
                    "have": target.prior_hours + progress.counted_hours,
                    "goal": target.goal_hours,
                    "partial": progress.partial_hours,
                }
    except Exception:
        return None
    return None


def _events(settings):
    # ALP answers get_event_state from its own cache. The events describe the
    # automation's scope, because the state they feed into does.
    reply = dashboard._front().do_action_device("get_event_state", settings.device_num, {})
    return dashboard.event_map(reply)


def build(telescope_id) -> dict:
    """What a render of the page or /tonight/state needs. Sends nothing to the scope."""
    now = _now()
    clock = dashboard.site_clock
    status = dashboard.get_automation().status()
    settings = roof_config.load()
    events = _events(settings)
    plan = gather_plan(status, settings, now, clock)
    stack = stack_summary(status, plan["log_rows"], _goal(status))
    here = settings.device_num == telescope_id
    state = tonight_state(
        status,
        events,
        stack,
        plan,
        now=now,
        clock=clock,
        stop_requested_at=_stop_requested_at,
        here=here,
    )
    return {
        "state": state,
        "status": status,
        "roof_lines": roof_status(status),
        "line": night_line(plan["window"], plan["blocks"], now, clock, _roof_record(plan, settings, now)),
        "plan": plan,
        "steps": dashboard.tracker_steps(status, events)["steps"] if state["steps"] else [],
        "details": details_rows(status, stack, events, clock),
        "updated": clock(now),
        "uses_target_list": status.get("uses_target_list"),
        "schedule_name": schedule_tools.label(status.get("schedule") or "").replace(".json", ""),
    }


def _on_accent(colour):
    """Black or white, whichever reads on a #rrggbb accent. None if it isn't one."""
    text = str(colour or "").strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        red, green, blue = (int(text[i : i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return None
    channels = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in (red, green, blue)]
    luminance = 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]
    return "white" if 1.05 / (luminance + 0.05) >= (luminance + 0.05) / 0.05 else "black"


def _first_images(telescope_id):
    """What's already held, so a reload shows it at once. Fetches nothing."""
    stack = dashboard._store(telescope_id).latest(None)
    pier = dashboard._pier_store.peek()
    return {
        "stack": dashboard._image_meta(stack) if stack else None,
        "pier": dashboard.pier_meta(pier) if pier else None,
    }


def first_layout(hero, images) -> dict:
    """Which image leads on a phone before tonight.js has run. It then keeps this up
    to date, including when the user swaps, so the page doesn't jump as it loads."""
    has_stack, has_pier = bool(images["stack"]), bool(images["pier"])
    front = hero
    if front == "stack" and not has_stack:
        front = "pier"
    if front == "pier" and not has_pier:
        front = "stack" if has_stack else "none"
    if front in ("focus", "plan"):
        front = "none"
    return {"front": front, "layout": "thumbs" if front == "none" else "below"}


class TonightPage:
    @staticmethod
    def on_get(req, resp, telescope_id=1):
        front = dashboard._front()
        context = dict(front.get_context(telescope_id, req))
        context["partial_path"] = "dashboard"
        built = build(telescope_id)
        images = _first_images(telescope_id)
        focusing = _focus_chart(telescope_id)
        layout = first_layout("focus" if focusing else built["state"]["hero"], images)
        front.render_template(
            req,
            resp,
            "ssc/tonight.html",
            page_root=f"/{telescope_id}/dashboard",
            state_url=f"/{telescope_id}/tonight/state",
            focus_url=f"/{telescope_id}/tonight/focus",
            hardware_url=f"/{telescope_id}/tonight/hardware",
            auth_url=f"/{telescope_id}/tonight/auth",
            stop_url=f"/{telescope_id}/roof/stop",
            on_accent=_on_accent(context.get("webui_accent_color")),
            images=images,
            first_layout=layout,
            first_focus=focusing,
            **built,
            **context,
        )


class TonightState:
    """The polled state. Only ever reads the loop's snapshot and cached events."""

    @staticmethod
    def on_get(req, resp, telescope_id=1):
        dashboard._front().render_fragment(
            req,
            resp,
            "ssc/tonight_state.html",
            root=f"/{telescope_id}",
            **build(telescope_id),
        )
        resp.set_header("Cache-Control", "no-store")


def _focus_chart(telescope_id):
    run = focus.focus_log(telescope_id).current()
    return focus.chart(run) if run else None


class TonightFocus:
    """The autofocus curve while a run is showing. Empty otherwise, which is what puts
    the image back. Only reads what roof.focus has already kept."""

    @staticmethod
    def on_get(req, resp, telescope_id=1):
        front = dashboard._front()
        front.render_fragment(req, resp, "ssc/focus_chart.html", chart=_focus_chart(telescope_id))
        resp.text = resp.text.strip()


class TonightHardware:
    """Costs several calls to the scope, so it runs only while Details is open."""

    @staticmethod
    def on_get(req, resp, telescope_id=1):
        front = dashboard._front()
        stats = front.get_device_state(telescope_id)
        rows = dashboard.device_rows(stats)
        if stats and not rows:
            # The scope is online but didn't answer, which can happen while it images.
            resp.status = falcon.HTTP_204
            return
        front.render_fragment(req, resp, "ssc/tonight_hardware.html", rows=rows)


class TonightAuth:
    """Same check as /auth-status, whose reply has Bootstrap markup. Kept apart from
    /tonight/state because the check can ask the scope, at most once per its cache."""

    @staticmethod
    def on_get(req, resp, telescope_id=1):
        front = dashboard._front()
        if telescope_id > 0 and front.check_needs_auth(telescope_id):
            front.render_fragment(req, resp, "ssc/auth_notice.html", needs_auth_warning=True)
            return
        resp.status = falcon.HTTP_200
        resp.content_type = "text/html"
        resp.text = ""


PAGES = {
    "dashboard": TonightPage,
    "tonight/state": TonightState,
    "tonight/focus": TonightFocus,
    "tonight/hardware": TonightHardware,
    "tonight/auth": TonightAuth,
}
