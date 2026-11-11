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

    def progress(self, target) -> Progress:
        """Totals for a target. A framed run counts when its image came out covered, or
        with no image to judge, once it ran an hour."""
        key = base_name(target.name)
        counted = partial = wall = stacked_s = 0.0
        sessions = 0
        for row in self.rows():
            if row["target"] != key:
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
