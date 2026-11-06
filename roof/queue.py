"""The target list: what we want to image, and how much of it.

Stored as JSON beside config.toml. Coordinates are J2000, RA in decimal hours.
Framing comes from the Seestar app's plans (Sky Atlas framing) or a saved schedule.
"""

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, fields

from roof import schedule_tools

PRIORITIES = ("low", "normal", "high")
DEFAULT_GOAL_HOURS = 10.0
DEFAULT_SINGLE_SESSION_HOURS = 1.0
# A framed mosaic is one stack that the firmware spreads across its panels, so a
# short run leaves uneven coverage. Used only until the scope has given an estimate
# for that size of framing; 4 h matches what the Cygnus mosaics were scheduled for.
DEFAULT_FRAMED_SESSION_HOURS = 4.0
# setting.mosaic.estimated_hours, from the scope's logs on 2026-11-05. It stayed the
# same across angles, targets and exposures, so it's kept per scale.
KNOWN_FRAMING_HOURS = {"1.70": 1.375}
DEFAULT_EXPOSURE_S = 30.0
DEFAULT_GAIN = 200


@dataclass
class Target:
    id: str
    name: str
    ra: float
    dec: float
    goal_hours: float = DEFAULT_GOAL_HOURS
    min_session_hours: float = DEFAULT_SINGLE_SESSION_HOURS
    prior_hours: float = 0.0  # integration from before this list existed
    priority: str = "normal"
    enabled: bool = True
    lp_filter: bool = True
    gain: int = DEFAULT_GAIN
    exposure_s: float = DEFAULT_EXPOSURE_S
    mosaic_scale: float = 1.0
    mosaic_angle: float = 0.0
    mosaic_star_map_angle: float | None = None
    moon_sep_deg: float | None = None  # None: the default for its filter
    moon_width_days: float | None = None
    source: str = ""

    @property
    def framed(self) -> bool:
        return self.mosaic_scale > 1.0


def _key(name: str) -> str:
    return " ".join(str(name).lower().split())


def _coerce(data: dict) -> Target | None:
    known = {f.name for f in fields(Target)}
    try:
        return Target(**{k: v for k, v in data.items() if k in known})
    except TypeError:
        return None


def with_framing(target: dict, mosaic: dict | None) -> dict:
    if mosaic and float(mosaic.get("scale", 1.0)) > 1.0:
        target["mosaic_scale"] = round(float(mosaic["scale"]), 2)
        target["mosaic_angle"] = schedule_tools.norm_angle(mosaic.get("angle", 0.0))
        if mosaic.get("star_map_angle") is not None:
            target["mosaic_star_map_angle"] = float(mosaic["star_map_angle"])
        target["min_session_hours"] = DEFAULT_FRAMED_SESSION_HOURS
    return target


def targets_from_plan(plan: dict, source: str = "Seestar plan") -> list[dict]:
    """Target fields from a Seestar app plan, keeping its framing."""
    found = []
    for entry in (plan or {}).get("list") or []:
        ra_dec = entry.get("target_ra_dec") or []
        if len(ra_dec) != 2 or list(ra_dec) == [-1.0, -1.0]:
            continue
        found.append(
            with_framing(
                {
                    "name": entry.get("alias_name") or entry.get("target_name") or "target",
                    "ra": round(float(ra_dec[0]), 4),
                    "dec": round(float(ra_dec[1]), 4),
                    "lp_filter": bool(entry.get("lp_filter", False)),
                    "source": source,
                },
                entry.get("mosaic"),
            )
        )
    return found


def targets_from_schedule(items: list, source: str) -> list[dict]:
    """Target fields from a stored schedule, skipping items at the current position."""
    exposure_s = None
    found = []
    for item in items:
        action, params = item.get("action"), item.get("params") or {}
        if action == "set_setting":
            stack_l = (params.get("exp_ms") or {}).get("stack_l")
            exposure_s = stack_l / 1000 if stack_l else exposure_s
        if action not in ("start_mosaic", "start_framed_mosaic"):
            continue
        if params.get("ra") in (None, -1) or isinstance(params.get("ra"), str):
            continue
        target = {
            "name": params.get("target_name", "target"),
            "ra": float(params["ra"]),
            "dec": float(params["dec"]),
            "lp_filter": bool(params.get("is_use_lp_filter", False)),
            "gain": int(params.get("gain", DEFAULT_GAIN)),
            "source": source,
        }
        if exposure_s:
            target["exposure_s"] = exposure_s
        framing = None
        if action == "start_framed_mosaic":
            framing = {
                "scale": params.get("mosaic_scale", 1.0),
                "angle": params.get("mosaic_angle", 0.0),
                "star_map_angle": params.get("mosaic_star_map_angle"),
            }
        found.append(with_framing(target, framing))
    return found


class FramingEstimates:
    """The scope's own time estimate for each size of framing.

    The firmware only reports it for the framing it's running, so sizes are learned
    as they run. Losing the file only loses what can be learned again.
    """

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()

    @staticmethod
    def _key(scale: float) -> str:
        return f"{float(scale):.2f}"

    def _read(self) -> dict:
        learned = dict(KNOWN_FRAMING_HOURS)
        try:
            with open(self._path) as f:
                doc = json.load(f)
            if isinstance(doc, dict):
                learned.update(doc)
        except (OSError, ValueError):
            pass
        return learned

    def hours(self, scale: float) -> float | None:
        with self._lock:
            value = self._read().get(self._key(scale))
        return float(value) if isinstance(value, (int, float)) and value > 0 else None

    def note(self, scale: float, hours: float) -> bool:
        """Remember an estimate. True if it was new."""
        with self._lock:
            learned = self._read()
            if learned.get(self._key(scale)) == hours:
                return False
            learned[self._key(scale)] = hours
            tmp = f"{self._path}.tmp"
            with open(tmp, "w") as f:
                json.dump(learned, f, indent=1)
            os.replace(tmp, self._path)
        return True


def apply_estimates(found: list[dict], estimates: FramingEstimates) -> list[dict]:
    """Start new framed targets at the scope's estimate for their size, if known."""
    for target in found:
        if target.get("mosaic_scale", 1.0) > 1.0:
            hours = estimates.hours(target["mosaic_scale"])
            if hours:
                target["min_session_hours"] = hours
    return found


class TargetQueue:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()

    def all(self) -> list[Target]:
        with self._lock:
            return self._read()

    def _read(self) -> list[Target]:
        try:
            with open(self._path) as f:
                doc = json.load(f)
        except FileNotFoundError:
            return []
        except ValueError as e:
            # Saving over a file we can't read would destroy the list.
            raise RuntimeError(f"target list {self._path} is unreadable: {e}") from e
        rows = doc.get("targets", []) if isinstance(doc, dict) else []
        return [t for t in (_coerce(r) for r in rows if isinstance(r, dict)) if t]

    def _write(self, targets: list[Target]) -> None:
        tmp = f"{self._path}.tmp"
        with open(tmp, "w") as f:
            json.dump({"targets": [asdict(t) for t in targets]}, f, indent=2)
        os.replace(tmp, self._path)

    def merge(self, found: list[dict], goal_hours: float | None = None) -> tuple[int, int]:
        """Add new targets; refresh coordinates and framing of ones already listed.

        A listed target keeps its goal and settings, so its progress still means
        the same thing. Returns (added, updated).
        """
        added = updated = 0
        with self._lock:
            targets = self._read()
            by_name = {_key(t.name): t for t in targets}
            for fields_ in found:
                existing = by_name.get(_key(fields_["name"]))
                if existing is None:
                    target = Target(id=uuid.uuid4().hex[:10], **fields_)
                    if goal_hours is not None:
                        target.goal_hours = goal_hours
                    targets.append(target)
                    by_name[_key(target.name)] = target
                    added += 1
                    continue
                for field_ in ("ra", "dec", "mosaic_scale", "mosaic_angle", "mosaic_star_map_angle"):
                    if field_ in fields_:
                        setattr(existing, field_, fields_[field_])
                updated += 1
            self._write(targets)
        return added, updated

    def update(self, target_id: str, **changes) -> bool:
        with self._lock:
            targets = self._read()
            for target in targets:
                if target.id == target_id:
                    for key, value in changes.items():
                        setattr(target, key, value)
                    self._write(targets)
                    return True
        return False

    def remove(self, target_id: str) -> bool:
        with self._lock:
            targets = self._read()
            kept = [t for t in targets if t.id != target_id]
            if len(kept) == len(targets):
                return False
            self._write(kept)
        return True
