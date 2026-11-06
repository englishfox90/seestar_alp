"""Record of when the roof read safe or unsafe, for the Night Line.

Only transitions are stored, each with its time, so a quiet month is a few rows.
The automation calls record() when the roof changes. Nothing here reads the safety
monitor or decides anything, and the loop treats a failure here as bookkeeping.

A reading is "safe", "unsafe" (the monitor answered that it isn't safe), or
"unknown" (no answer, or not connected). Automation treats unknown as unsafe, so
both count as closed here too, and only the period's reason tells them apart.

The record only sees what the automation saw: while the app is down nothing is
written, so a period that spans a restart is drawn as one, and a closure that
began and ended during the outage isn't there at all.
"""

import json
import os
import threading
from datetime import datetime, timedelta, timezone

SAFE, UNSAFE, UNKNOWN = "safe", "unsafe", "unknown"
KEEP_S = 30 * 86400
# A monitor that flaps on every tick mustn't grow the file a tick has to write.
MAX_ROWS = 2000


def reading_state(reading) -> str:
    """Reduce a SafetyReading to what's stored."""
    if reading.safe:
        return SAFE
    return UNSAFE if reading.connected is True else UNKNOWN


def night_window(night: str, lon: float) -> tuple[float, float]:
    """Start and end of an observing night: the inverse of sun.night_key."""
    midnight = datetime.fromisoformat(night).replace(tzinfo=timezone.utc)
    start = midnight + timedelta(hours=12 - lon / 15.0)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def _valid(row) -> bool:
    return (
        isinstance(row, dict)
        and isinstance(row.get("at"), (int, float))
        and row.get("state") in (SAFE, UNSAFE, UNKNOWN)
    )


class NightHistory:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._rows = self._read()
        self._dirty = False

    def _read(self) -> list[dict]:
        # Built while the automation starts, so no state of this file may stop that.
        try:
            with open(self._path) as f:
                rows = json.load(f)
        except OSError:
            return []
        except ValueError:
            # Keep the damaged file, as SessionLog does.
            try:
                os.replace(self._path, f"{self._path}.damaged")
            except OSError:
                pass
            return []
        return [r for r in rows if _valid(r)] if isinstance(rows, list) else []

    def record(self, reading, night: str, now: float) -> None:
        """Note a roof reading. A failed write raises for the caller to log, and
        the row stays in memory to go out with the next call."""
        state = reading_state(reading)
        with self._lock:
            # The automation reports its first reading after every restart.
            if not self._rows or self._rows[-1]["state"] != state:
                self._rows.append({"at": now, "night": night, "state": state})
                self._prune(now)
                self._dirty = True
            if self._dirty:
                self._save()

    def _prune(self, now):
        # The newest expired row stays: it says what the roof was at the cutoff.
        expired = sum(1 for r in self._rows if r["at"] < now - KEEP_S)
        del self._rows[: max(0, expired - 1)]
        del self._rows[:-MAX_ROWS]

    def _save(self):
        tmp = f"{self._path}.tmp"
        with open(tmp, "w") as f:
            json.dump(self._rows, f, indent=1)
        os.replace(tmp, self._path)
        self._dirty = False

    def _sorted_rows(self) -> list[dict]:
        with self._lock:
            rows = list(self._rows)
        return sorted(rows, key=lambda r: r["at"])

    def recording_since(self) -> float | None:
        """The oldest reading held. Before it, the roof's state isn't known."""
        rows = self._sorted_rows()
        return rows[0]["at"] if rows else None

    def closed_periods(self, night: str, lon: float, now: float) -> list[dict]:
        """Times the roof read closed during a night, clipped to that night.

        Each is {start, end, open, reason}. A period still running has end None and
        open True, which only happens during the current night. The reason is
        "unsafe" if the monitor said so at any recorded point, otherwise "unknown".
        """
        night_start, night_end = night_window(night, lon)
        if now < night_start:
            return []
        periods = []
        current = None
        for row in self._sorted_rows():
            if row["at"] >= night_end or row["at"] > now:
                break
            if row["state"] == SAFE:
                if current:
                    periods.append({**current, "end": row["at"], "open": False})
                    current = None
            elif current is None:
                current = {"start": row["at"], "reason": row["state"]}
            elif row["state"] == UNSAFE:
                current["reason"] = UNSAFE
        if current:
            running = now < night_end
            periods.append(
                {**current, "end": None if running else night_end, "open": running}
            )
        return [
            {
                "start": max(p["start"], night_start),
                "end": p["end"],
                "open": p["open"],
                "reason": p["reason"],
            }
            for p in periods
            if p["open"] or p["end"] > night_start
        ]
