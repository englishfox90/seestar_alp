"""What the night's two longer Discord posts say: the briefing before dark, and the
report after dawn.

Each builder returns a message (title, text, fields, picture) or None, and never
raises: a missing forecast, plan or image only leaves that part out. They read the
same sources Tonight does, through ``Sources``, so tests can hand in their own.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from roof import alert_images, planner, schedule_tools, sky_chart
from roof.alerts import find_stack, health_text
from roof.sessions import base_name
from roof.sun import night_key

MOON_STEP_S = 10 * 60
PLAN_LINES = 8
SKIPPED_LINES = 4
REPORT_TARGETS = 8
FIELD_MAX = 1024  # Discord's limit for a field's value

_TONE_LEVEL = {"ok": "success", "attention": "warning", "danger": "warning"}


@dataclass
class Sources:
    settings: Callable
    clock: Callable  # timestamp -> "HH:MM" in the site's zone
    tz: Callable  # -> the site's tzinfo
    targets: Callable  # -> the target list's targets
    log: Callable  # -> SessionLog or None
    history: Callable  # -> NightHistory or None
    outlook: Callable  # (settings, now) -> roof.forecast.latest's result, or None
    open_chance: Callable  # (settings, now) -> chance(when), or None
    image_dir: Callable  # -> the scope's MyWorks folder, or ""


def default_sources() -> Sources:
    """The running app's sources. Imported late: these modules need the app started."""
    from roof import config as roof_config
    from roof import dashboard, forecast

    def tz():
        return dashboard._front().site_timezone()

    def automation_history():
        return getattr(dashboard.get_automation(), "_night_history", None)

    return Sources(
        settings=roof_config.load,
        clock=dashboard.site_clock,
        tz=tz,
        targets=lambda: dashboard.target_queue().all(),
        log=dashboard.session_log,
        history=automation_history,
        outlook=lambda s, now: forecast.latest(s, dashboard.site_clock, tz(), now),
        open_chance=lambda s, now: forecast.open_chance(s, tz(), now),
        image_dir=lambda: roof_config.load().discord_image_dir,
    )


def _field(name, lines, inline=False):
    value = "\n".join(line for line in lines if line).strip()
    if not value:
        return None
    if len(value) > FIELD_MAX:
        value = value[: FIELD_MAX - 1] + "…"
    return {"name": name, "value": value, "inline": inline}


def _hours(seconds) -> str:
    return f"{seconds / 3600:.1f} h"


def _night_label(night, tz) -> str:
    day = datetime.fromisoformat(night)
    return f"{day:%a} {day.day} {day:%b}"


# ---- the sky ---------------------------------------------------------------------


def moon(window, lat, lon, clock):
    """(lit percent, spans the moon is up within the timeline, one line of text)."""
    sunset, sunrise = window.get("sunset"), window.get("sunrise")
    start, end = sunset - alert_images.MARGIN_S, sunrise + alert_images.MARGIN_S
    times = [
        start + k * MOON_STEP_S for k in range(int((end - start) // MOON_STEP_S) + 1)
    ]
    alt = sky_chart.altitudes(lat, lon, times)
    lit = max(alt["moon_lit"]) if alt["moon_lit"] else 0.0
    spans, began = [], None
    for when, a in zip(times, alt["moon"]):
        if a > 0 and began is None:
            began = when
        elif a <= 0 and began is not None:
            spans.append((began, when))
            began = None
    if began is not None:
        spans.append((began, times[-1]))
    dark_start, dark_end = (
        window.get("dark_start") or sunset,
        window.get("dark_end") or sunrise,
    )
    in_dark = [
        (max(a, dark_start), min(b, dark_end))
        for a, b in spans
        if b > dark_start and a < dark_end
    ]
    if not in_dark:
        text = f"Moon {lit:.0f}% lit, down while it's dark"
    elif (
        in_dark[0][0] <= dark_start + 600
        and in_dark[-1][1] >= dark_end - 600
        and len(in_dark) == 1
    ):
        text = f"Moon {lit:.0f}% lit, up all night"
    else:
        parts = []
        for a, b in in_dark[:2]:
            if a <= dark_start + 600:
                parts.append(f"up until {clock(b)}")
            elif b >= dark_end - 600:
                parts.append(f"rises {clock(a)}")
            else:
                parts.append(f"up {clock(a)} to {clock(b)}")
        text = f"Moon {lit:.0f}% lit, " + ", then ".join(parts)
    return lit, spans, text


def _outlook_for(src, settings, night, now):
    try:
        found = src.outlook(settings, now)
    except Exception:
        return None
    for entry in (found or {}).get("nights") or ():
        if entry.get("night") == night:
            return entry
    return None


def _open_hours(outlook, window) -> float | None:
    """Forecast hours of open roof inside the dark window."""
    if not outlook or not outlook.get("hours"):
        return None
    a, b = window.get("dark_start"), window.get("dark_end")
    if a is None or b is None:
        return None
    seconds = sum(
        max(0.0, min(h["end"], b) - max(h["start"], a))
        for h in outlook["hours"]
        if h.get("state") == "open"
    )
    return seconds / 3600


# ---- the plan --------------------------------------------------------------------


def _goal(target, log) -> tuple[float, float] | None:
    if target is None or log is None:
        return None
    try:
        return target.prior_hours + log.progress(
            target
        ).counted_hours, target.goal_hours
    except Exception:
        return None


def _plan(src, settings, night, now, offset=0):
    """(blocks, skipped) for a night from the target list, as plain rows. Blocks are
    dicts with name, start, end, goal; skipped are (name, why)."""
    targets = list(src.targets() or ())
    log = src.log()
    if not targets or log is None:
        return [], []
    cfg = planner.config_from_settings(settings)
    try:
        chance = src.open_chance(settings, now)
    except Exception:
        chance = None
    plans = planner.forecast(targets, log, cfg, now, offset + 1, open_chance=chance)
    plan = plans[offset]
    if plan.key != night:
        return [], []
    by_id = {t.id: t for t in targets}
    blocks = []
    for block in plan.blocks:
        goal = _goal(by_id.get(block.target.id), log)
        blocks.append(
            {
                "name": block.target.name,
                "start": block.start,
                "end": block.end,
                "goal": goal,
            }
        )
    skipped = [(by_id[i].name, why) for i, why in plan.skipped.items() if i in by_id]
    return blocks, skipped


def _plan_lines(blocks, clock):
    lines = []
    for b in blocks[:PLAN_LINES]:
        line = f"{clock(b['start'])}–{clock(b['end'])}  **{b['name']}**"
        if b.get("goal"):
            have, goal = b["goal"]
            line += f"  ({have:.1f} of {goal:g} h)"
        lines.append(line)
    if len(blocks) > PLAN_LINES:
        lines.append(f"…and {len(blocks) - PLAN_LINES} more")
    return lines


def _schedule_names(settings):
    path = schedule_tools.resolve(settings.schedule) if settings.schedule else None
    if not path:
        return []
    items, errors, _warnings = schedule_tools.load(path)
    return [] if errors else schedule_tools.target_names(items)


# ---- the briefing ----------------------------------------------------------------


def briefing(night, now, health=None, src: Sources | None = None) -> dict | None:
    """The post before dark: roof forecast, the dark hours and moon, the plan, and
    the scope's health. None when the site isn't set."""
    src = src or default_sources()
    settings = src.settings()
    if settings.lat == 0 and settings.lon == 0:
        return None
    clock = src.clock
    window = planner.night_times(
        night, settings.lat, settings.lon, settings.sun_max_deg
    )
    if window.get("dark_start") is None:
        return None
    outlook = _outlook_for(src, settings, night, now)
    lit, moon_spans, moon_line = moon(window, settings.lat, settings.lon, clock)
    dark_h = (window["dark_end"] - window["dark_start"]) / 3600
    lines = [
        f"Dark {clock(window['dark_start'])} to {clock(window['dark_end'])} ({dark_h:.1f} h)",
        moon_line,
    ]
    if outlook and outlook.get("dew"):
        lines.append(outlook["dew"])

    if outlook:
        title = f"Tonight: {outlook['headline']}"
        level = _TONE_LEVEL.get(outlook.get("tone"), "info")
        roof = _field("Roof forecast", [outlook.get("detail") or outlook["headline"]])
        if outlook.get("key") == "closed":
            lines.insert(0, "No imaging expected tonight.")
    else:
        title, level, roof = (
            "Tonight",
            "info",
            _field("Roof forecast", ["No forecast yet."]),
        )

    blocks, skipped = [], []
    if settings.schedule == schedule_tools.TARGET_LIST:
        try:
            blocks, skipped = _plan(src, settings, night, now)
        except Exception as e:
            logging.getLogger("roof").warning(
                "briefing: could not plan the night: %s", e
            )
        plan = _field(
            "Plan",
            _plan_lines(blocks, clock)
            or ["Nothing on the target list can be imaged tonight."],
        )
        left_out = _field(
            "Not tonight",
            [f"{name}: {why}" for name, why in skipped[:SKIPPED_LINES]]
            + (
                [f"…and {len(skipped) - SKIPPED_LINES} more"]
                if len(skipped) > SKIPPED_LINES
                else []
            ),
        )
    else:
        names = _schedule_names(settings)
        label = schedule_tools.label(settings.schedule) or "No schedule chosen"
        plan = _field(
            f"Plan: {label}", [", ".join(names)] if names else ["No targets."]
        )
        left_out = None
    if not settings.enabled:
        lines.append("Automation is off, so nothing will start on its own.")

    fields = [
        f
        for f in (
            roof,
            plan,
            left_out,
            _field("Scope", [health_text(health)]) if health else None,
        )
        if f
    ]
    picture = None
    try:
        picture = alert_images.timeline(
            window, (outlook or {}).get("hours") or (), blocks, moon_spans, lit, clock
        )
    except Exception as e:
        logging.getLogger("roof").warning(
            "briefing: could not draw the timeline: %s", e
        )
    return {
        "title": title,
        "text": "\n".join(lines),
        "level": level,
        "fields": fields,
        "image": ("tonight.png", picture, "image/png") if picture else None,
        "forecast": {
            "headline": outlook["headline"] if outlook else None,
            "open_h": _open_hours(outlook, window),
        },
    }


# ---- the morning report ----------------------------------------------------------


def _roof_open_hours(src, settings, night, window, now):
    """(open hours in the dark window by the roof record, dark hours), or None."""
    history = src.history()
    a, b = window.get("dark_start"), window.get("dark_end")
    if history is None or a is None or b is None:
        return None
    try:
        record = history.roof_record(night, settings.lon, now)
    except Exception:
        return None
    end = min(b, now)
    if end <= a:
        return None
    lost = 0.0
    spans = [
        (p["start"], p["end"] if p["end"] is not None else end)
        for p in record.get("closed", ())
    ]
    spans += [(u["start"], u["end"]) for u in record.get("unknown", ())]
    merged = []
    for s, e in sorted((max(s, a), min(e, end)) for s, e in spans):
        if e <= s:
            continue
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    lost = sum(e - s for s, e in merged)
    return (end - a - lost) / 3600, (b - a) / 3600


def _target_lines(name, totals, target, log):
    frames, dropped, seconds = (
        totals.get("frames", 0),
        totals.get("dropped", 0),
        totals.get("seconds", 0.0),
    )
    if not frames:
        return ["Nothing stacked."]
    share = f" ({dropped * 100 / (frames + dropped):.0f}%)" if dropped else ""
    lines = [
        f"{frames} frames, {_hours(seconds)}",
        f"{dropped} dropped{share}" if dropped else "None dropped",
    ]
    goal = _goal(target, log)
    if goal:
        have, wanted = goal
        lines.append(f"Goal {have:.1f} of {wanted:g} h (+{seconds / 3600:.1f} tonight)")
    return lines


def morning_report(
    night_state, now, health=None, src: Sources | None = None
) -> dict | None:
    """The post after dawn: each target's frames and goal progress, the roof against
    its forecast, alarms, the stacks together, and the next night."""
    src = src or default_sources()
    settings = src.settings()
    night = night_state.get("night")
    if not night:
        return None
    clock = src.clock
    window = {}
    if settings.lat or settings.lon:
        window = planner.night_times(
            night, settings.lat, settings.lon, settings.sun_max_deg
        )
    targets_done = night_state.get("targets") or {}
    total_s = sum(t.get("seconds", 0.0) for t in targets_done.values())
    imaged = night_state.get("imaged") and total_s > 0
    label = _night_label(night, None)

    fields = []
    if imaged:
        count = sum(1 for t in targets_done.values() if t.get("frames"))
        title = f"Night of {label}: {_hours(total_s)} on {count} target{'s' if count != 1 else ''}"
        level, lines = "success", []
    else:
        title, level = f"Night of {label}: no imaging", "warning"
        if night_state.get("sessions"):
            lines = ["A session started, but nothing was stacked before dawn."]
        else:
            note = night_state.get("last_note") or ""
            lines = [
                note.removeprefix("waiting: ").capitalize() + "."
                if note
                else "It never started."
            ]

    by_key = {}
    log = src.log()
    try:
        by_key = {base_name(t.name): t for t in src.targets() or ()}
    except Exception:
        pass
    for name, totals in list(targets_done.items())[:REPORT_TARGETS]:
        field = _field(
            name,
            _target_lines(name, totals, by_key.get(base_name(name)), log),
            inline=True,
        )
        if field:
            fields.append(field)

    roof_lines = []
    if window.get("dark_start") is not None:
        found = _roof_open_hours(src, settings, night, window, now)
        if found:
            open_h, dark_h = found
            roof_lines.append(f"Open {open_h:.1f} h of {dark_h:.1f} h dark")
    forecast = night_state.get("forecast") or {}
    if forecast.get("headline"):
        expected = (
            f" ({forecast['open_h']:.1f} h open)"
            if forecast.get("open_h") is not None
            else ""
        )
        roof_lines.append(f"Forecast said: {forecast['headline']}{expected}")
    roof = _field("Roof", roof_lines)
    if roof:
        fields.append(roof)

    alarms = night_state.get("alarms") or []
    if alarms:
        fields.append(
            _field("Alarms", [f"{clock(a['at'])}  {a['message']}" for a in alarms[-5:]])
        )
        if level == "success":
            level = "warning"

    following = _next_night(src, settings, night, now, clock)
    if following:
        fields.append(following)

    picture = None
    directory = src.image_dir()
    if directory and imaged:
        stacks = []
        for row in night_state.get("stacks") or ():
            path = find_stack(directory, row.get("name"), row.get("started", 0))
            if path:
                stacks.append((row["name"], path))
        try:
            picture = alert_images.collage(stacks)
        except Exception as e:
            logging.getLogger("roof").warning(
                "report: could not make the collage: %s", e
            )
    return {
        "title": title,
        "text": "\n".join(lines),
        "level": level,
        "fields": [f for f in fields if f],
        "image": ("night.jpg", picture, "image/jpeg") if picture else None,
    }


def _next_night(src, settings, night, now, clock):
    if not (settings.lat or settings.lon):
        return None
    following = (datetime.fromisoformat(night) + timedelta(days=1)).date().isoformat()
    lines = []
    outlook = _outlook_for(src, settings, following, now)
    if outlook:
        lines.append(outlook["headline"])
    if settings.schedule == schedule_tools.TARGET_LIST:
        try:
            offset = 1 if night_key(now, settings.lon) == night else 0
            blocks, _skipped = _plan(src, settings, following, now, offset)
        except Exception:
            blocks = []
        if blocks:
            lines.append("Planned: " + ", ".join(b["name"] for b in blocks[:5]))
    return _field("Next night", lines)
