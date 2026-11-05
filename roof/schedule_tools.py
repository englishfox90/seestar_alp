"""Stored schedules: listing, validation, and app-plan conversion."""

import json
import os
import re

# Imaging defaults for plans captured from the Seestar app, matched to the prior
# app session (gain 200, 30 s subs).
PANEL_TIME_SEC = 3600
GAIN = 200
STACK_EXPOSURE_MS = 30000
PANEL_OVERLAP = 20

# Items replayed before a mid-schedule resume, because skipping them would leave
# the settings the start-up sequence reset from config.toml.
SETTING_ACTIONS = {
    "set_setting",
    "set_setting_exposures",
    "action_set_exposure",
    "action_set_dew_heater",
}
IMAGING_ACTIONS = {"start_mosaic", "start_framed_mosaic", "start_spectra"}
# This rig is permanently polar-aligned and must stay powered.
FORBIDDEN_ACTIONS = {"start_polar_align", "shutdown", "pi_shutdown", "pi_reboot"}


def schedule_dir() -> str:
    """Same folder ALP's Schedule -> Import dropdown lists."""
    return os.path.join(os.getcwd(), "schedule")


def list_schedules(directory: str | None = None) -> list[str]:
    directory = directory or schedule_dir()
    if not os.path.isdir(directory):
        return []
    return sorted(
        f
        for f in os.listdir(directory)
        if f.endswith(".json") and os.path.isfile(os.path.join(directory, f))
    )


def resolve(name: str, directory: str | None = None) -> str | None:
    """Absolute path of a listed schedule. Only listed names resolve."""
    directory = directory or schedule_dir()
    return os.path.join(directory, name) if name in list_schedules(directory) else None


def validate_items(items) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Any error blocks the schedule from running."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(items, list) or not items:
        return ["schedule has no items"], warnings
    has_target = False
    for number, item in enumerate(items, 1):
        if not isinstance(item, dict) or "action" not in item:
            errors.append(f"item {number}: not a schedule item")
            continue
        action, params = item["action"], item.get("params") or {}
        if action in FORBIDDEN_ACTIONS:
            errors.append(f"item {number}: {action} is not allowed on this rig")
        if action == "start_up_sequence" and params.get("3ppa"):
            errors.append(f"item {number}: start_up_sequence with 3ppa would re-align")
        if action in IMAGING_ACTIONS:
            has_target = True
            if "panel_time_sec" not in params:
                errors.append(
                    f"item {number}: {action} needs panel_time_sec "
                    "(session_time_sec is deprecated and breaks the Schedule page)"
                )
        if action == "set_setting_exposures":
            warnings.append(
                f"item {number}: set_setting_exposures has no handler in ALP and "
                "may not set the exposure; use set_setting instead"
            )
    if not has_target:
        warnings.append("schedule has no imaging targets")
    return errors, warnings


def load(path: str) -> tuple[list, list[str], list[str]]:
    """Return (items, errors, warnings) for a schedule file."""
    try:
        with open(path) as f:
            doc = json.load(f)
        items = doc["list"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        return [], [f"cannot read schedule: {e}"], []
    errors, warnings = validate_items(items)
    return items, errors, warnings


def target_names(items) -> list[str]:
    return [
        i.get("params", {}).get("target_name", "?")
        for i in items
        if isinstance(i, dict) and i.get("action") in IMAGING_ACTIONS
    ]


def describe_target(item: dict) -> str:
    """Short label for how a schedule item frames its target."""
    params = item.get("params", {})
    action = item.get("action")
    if action == "start_framed_mosaic":
        return f"Framed mosaic {params.get('mosaic_scale', 1.0):g}x at {params.get('mosaic_angle', 0.0):g}\u00b0"
    if action == "start_mosaic":
        ra_num, dec_num = params.get("ra_num", 1), params.get("dec_num", 1)
        if ra_num == 1 and dec_num == 1:
            return "Single frame"
        return f"Mosaic {ra_num}x{dec_num} panels"
    if action == "start_spectra":
        return "Spectra"
    return str(action)


def _norm_angle(angle) -> float:
    # ALP wants mosaic_angle in [-90, 90]; framing is symmetric under +/-180.
    angle = float(angle)
    while angle > 90:
        angle -= 180
    while angle < -90:
        angle += 180
    return round(angle, 2)


def _target_item(target: dict) -> dict:
    ra, dec = target.get("target_ra_dec", [-1.0, -1.0])
    params = {
        "target_name": target.get("target_name", "target"),
        "ra": round(float(ra), 4),
        "dec": round(float(dec), 4),
        "is_j2000": True,  # app plans store J2000
        "is_use_lp_filter": bool(target.get("lp_filter", False)),
        "is_use_autofocus": True,
        "panel_time_sec": PANEL_TIME_SEC,
        "gain": GAIN,
    }
    mosaic = target.get("mosaic")
    if mosaic and float(mosaic.get("scale", 1.0)) > 1.0:
        params["mosaic_scale"] = round(float(mosaic.get("scale", 1.0)), 2)
        params["mosaic_angle"] = _norm_angle(mosaic.get("angle", 0.0))
        if "star_map_angle" in mosaic:
            params["mosaic_star_map_angle"] = float(mosaic["star_map_angle"])
        return {"action": "start_framed_mosaic", "params": params}
    params.update(ra_num=1, dec_num=1, panel_overlap_percent=PANEL_OVERLAP)
    return {"action": "start_mosaic", "params": params}


def plan_to_schedule(plan: dict) -> dict:
    """Convert a Seestar app ViewPlan into a schedule document."""
    targets = plan.get("list") or []
    if not targets:
        raise ValueError("plan has no targets")
    exposure = {
        "action": "set_setting",
        "params": {"exp_ms": {"stack_l": STACK_EXPOSURE_MS, "continuous": 500}},
    }
    return {"list": [exposure] + [_target_item(t) for t in targets]}


def safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 _.-]", "", name or "").strip(" .")
    if not cleaned:
        raise ValueError("plan needs a name")
    return cleaned


def write_schedule(name: str, schedule: dict, directory: str | None = None) -> str:
    directory = directory or schedule_dir()
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{safe_name(name)}.json")
    with open(path, "w") as f:
        json.dump(schedule, f, indent=2)
    return path
