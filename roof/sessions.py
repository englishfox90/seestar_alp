"""Record of what actually got stacked, target by target.

Each stack run is one row, updated while it runs, so a crash loses at most a
minute. Its identity is (target, start time), and the start time comes from the
firmware's own stack clock, so an ALP restart continues the row instead of
counting the same frames twice.

Progress comes from frames stacked, not the schedule's time: on 2026-11-05 a
framed mosaic had stacked only 59% of its wall-clock time (253 of 345 frames kept
at 20 s, in 2.38 h).
"""

import json
import os
import re
import threading
from dataclasses import dataclass

SAVE_EVERY_S = 60
# A framed run is judged on its saved image once it has ended this long ago, to let the
# scope write it and the share sync it. After the give-up time it's judged on length.
COVERAGE_WAIT_S = 5 * 60
COVERAGE_GIVE_UP_S = 6 * 3600
# Without an image to judge, a framed run counts once it has run this long.
UNJUDGED_MIN_S = 3600
SAME_RUN_TOLERANCE_S = 300
_PANEL_SUFFIX = re.compile(r"_\d+$")


def base_name(view_target: str) -> str:
    """Target name without the _<ra><dec> panel suffix mosaics add."""
    return " ".join(_PANEL_SUFFIX.sub("", str(view_target)).lower().split())


def panel_of(view_target: str) -> str | None:
    """The two-digit panel a mosaic's stack name ends in, "21" for "M31_21"."""
    match = _PANEL_SUFFIX.search(str(view_target or ""))
    return match.group(0)[1:] if match and len(match.group(0)) == 3 else None


def camera_of(row) -> str:
    """Which camera a run was: "wide", or "main" for the telescope (older rows)."""
    return row.get("camera") or "main"


def counted_camera(target) -> str:
    """The camera whose stacks count toward a target: the wide angle camera for a
    wide target, otherwise the telescope (a "both" target is counted by it)."""
    return "wide" if getattr(target, "camera", "main") == "wide" and not target.framed else "main"


def counts(target, row) -> bool:
    """Whether a run counts toward its target's goal. Single frames always do. A framed
    run does when its saved image came out covered, or with no image judged, once it ran
    an hour."""
    if target is None or not target.framed:
        return True
    covered = row.get("covered")
    if covered is None:
        return row.get("wall_s", 0) >= UNJUDGED_MIN_S
    return bool(covered)


@dataclass(frozen=True)
class Progress:
    counted_hours: float = 0.0
    partial_hours: float = 0.0  # framed runs that left holes in the framing
    sessions: int = 0
    wall_hours: float = 0.0
    measured_rate: float | None = None  # stacked time / wall time


class SessionLog:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._rows = self._read()
        self._dirty_since = None

    def _read(self) -> list[dict]:
        try:
            with open(self._path) as f:
                rows = json.load(f)
        except FileNotFoundError:
            return []
        except ValueError:
            # Keep the damaged file: writing over it would lose every night's record.
            os.replace(self._path, f"{self._path}.damaged")
            return []
        return rows if isinstance(rows, list) else []

    def rows(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._rows]

    def observe(self, state, night: str, now: float) -> None:
        """Note the scope's current stack. Call once per automation tick."""
        with self._lock:
            stacking = (
                state.view_state == "working"
                and state.view_stage == "Stack"
                and state.view_target
                and state.stacked > 0
            )
            if stacking:
                self._note(state, night, now)
            wide = bool(getattr(state, "wide_target", "")) and getattr(state, "wide_stacked", 0) > 0
            if wide:
                self._note_wide(state, night, now)
            stacking = stacking or wide
            if self._dirty_since is not None and (
                not stacking or now - self._dirty_since >= SAVE_EVERY_S
            ):
                self._save()

    def _note(self, state, night, now):
        target = base_name(state.view_target)
        started = now - state.stack_lapse_s
        row = next(
            (
                r
                for r in reversed(self._rows[-20:])
                if r["target"] == target and abs(r["started"] - started) < SAME_RUN_TOLERANCE_S
            ),
            None,
        )
        if row is None:
            row = {"target": target, "name": state.view_target, "night": night, "started": started}
            self._rows.append(row)
        # Between exposures the firmware reports none, which mustn't wipe the run's hours.
        row["exposure_s"] = state.exposure_s or row.get("exposure_s", 0.0)
        row.update(
            stacked=state.stacked,
            dropped=state.dropped,
            wall_s=state.stack_lapse_s,
            scale=state.mosaic_scale or 1.0,
            last_seen=now,
        )
        if self._dirty_since is None:
            self._dirty_since = now

    # Without the firmware's elapsed time, a wide run is the same run while its
    # count keeps going and it was seen this recently.
    WIDE_GAP_S = 180

    def _note_wide(self, state, night, now):
        target, name = base_name(state.wide_target), state.wide_target
        lapse = state.wide_lapse_s
        row = None
        for r in reversed(self._rows[-20:]):
            if camera_of(r) != "wide" or r.get("name") != name:
                continue
            if lapse is not None:
                if abs(r["started"] - (now - lapse)) < SAME_RUN_TOLERANCE_S:
                    row = r
            elif now - r.get("last_seen", 0) <= self.WIDE_GAP_S and state.wide_stacked >= r.get("stacked", 0):
                row = r
            break
        if row is None:
            started = now - (lapse or 0.0)
            row = {"target": target, "name": name, "night": night, "started": started, "camera": "wide"}
            self._rows.append(row)
        row["exposure_s"] = state.wide_exposure_s or row.get("exposure_s", 0.0)
        row.update(
            stacked=state.wide_stacked,
            dropped=state.wide_dropped,
            wall_s=lapse if lapse is not None else now - row["started"],
            scale=1.0,
            last_seen=now,
        )
        if self._dirty_since is None:
            self._dirty_since = now

    def flush(self) -> None:
        with self._lock:
            if self._dirty_since is not None:
                self._save()

    def _save(self):
        tmp = f"{self._path}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self._rows, f, indent=1)
            os.replace(tmp, self._path)
            self._dirty_since = None
        except OSError:
            pass  # retried on the next tick; never let this reach the automation loop

    def review(self, directory: str, now: float, find=None, measure=None) -> int:
        """Judge ended framed runs by their saved image. Returns how many it judged.

        Done once per run, after it ends. Slow only the first time it meets old runs.
        """
        from roof import coverage

        find = find or coverage.run_stack
        measure = measure or coverage.black_share
        judged = 0
        with self._lock:
            for row in self._rows:
                if row.get("scale", 1.0) <= 1.0 or "covered" in row:
                    continue
                ended = row.get("last_seen", 0)
                if now - ended < COVERAGE_WAIT_S:
                    continue
                path = find(directory, row.get("name") or row["target"], ended) if directory else None
                if path:
                    share = measure(path)
                    row["black"] = round(share, 3)
                    row["covered"] = share <= coverage.COVERED_MAX_BLACK
                    row["stack"] = os.path.basename(path)
                elif now - ended >= COVERAGE_GIVE_UP_S:
                    row["covered"] = None  # no image to judge: its length decides
                else:
                    continue
                judged += 1
            if judged:
                self._save()
        return judged

    def panel_hours(self, target) -> dict:
        """Counted hours per panel of a mosaic target, every panel listed."""
        hours = {name: 0.0 for name in target.panel_names}
        key = base_name(target.name)
        for row in self.rows():
            panel = panel_of(row.get("name"))
            if row["target"] != key or camera_of(row) != counted_camera(target):
                continue
            if panel in hours and counts(target, row):
                hours[panel] += row["stacked"] * (row.get("exposure_s") or 0) / 3600
        return hours

    def progress(self, target) -> Progress:
        """Totals for a target. A framed run counts when its image came out covered, or
        with no image to judge, once it ran an hour."""
        key = base_name(target.name)
        counted = partial = wall = stacked_s = 0.0
        sessions = 0
        camera = counted_camera(target)
        for row in self.rows():
            if row["target"] != key or camera_of(row) != camera:
                continue
            hours = row["stacked"] * (row.get("exposure_s") or 0) / 3600
            wall += row.get("wall_s", 0)
            stacked_s += hours * 3600
            sessions += 1
            if counts(target, row):
                counted += hours
            else:
                partial += hours
        return Progress(
            counted_hours=counted,
            partial_hours=partial,
            sessions=sessions,
            wall_hours=wall / 3600,
            measured_rate=(stacked_s / wall) if wall >= 1800 else None,
        )
