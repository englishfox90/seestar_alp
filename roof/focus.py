"""Autofocus curves from the scope's own events, for the dashboard.

While it focuses, the scope sends an AutoFocus event about every 3 seconds, each
with every [focuser step, metric] pair so far. Once a curve has been fitted, the
last pair is where focus settled. ALP's event cache can't be used for this: a
"complete" event with no points replaces the curve about 2 seconds later. So the
points are kept here as they arrive, and nothing is ever sent to the scope.
"""

import logging
import math
import threading
import time

from blinker import signal

from device.config import Config

# Events come every 3 seconds or so, with up to about 8 before the first point. A
# run this quiet has been abandoned, and must not keep the image hidden.
RUN_TIMEOUT_S = 30
# Long enough for a browser polling every few seconds to see how a run ended.
LINGER_S = 60

_FINISHED = ("complete", "fail", "cancel")

WIDTH, HEIGHT = 640, 340
LEFT, RIGHT, TOP, BOTTOM = 44, 18, 30, 48


def _clean_points(points) -> list:
    """Keep well-formed [step, metric] pairs. A metric of 0 means no reading."""
    clean = []
    for point in points if isinstance(points, list) else []:
        if isinstance(point, (list, tuple)) and len(point) == 2:
            step, metric = point
            if (
                isinstance(step, (int, float))
                and isinstance(metric, (int, float))
                and math.isfinite(step)
                and math.isfinite(metric)
            ):
                clean.append((step, metric))
    return clean


class FocusLog:
    """The current or latest autofocus run for one scope."""

    def __init__(self, clock=time.monotonic):
        self._lock = threading.Lock()
        self._clock = clock
        self._run = None

    def on_event(self, event):
        # Runs on the thread that reads the scope's socket, which must survive
        # anything an unexpected event does here.
        try:
            self._record(event)
        except Exception:
            logging.getLogger("roof").exception("Could not record an autofocus event")

    def _new_run(self, now):
        return {
            "state": "working",
            "points": [],
            "curve": None,
            "lapse_ms": None,
            "error": None,
            "seen_at": now,
            "finished_at": None,
        }

    def _record(self, event):
        if not isinstance(event, dict) or event.get("Event") != "AutoFocus":
            return
        state = event.get("state")
        result = event.get("result")
        now = self._clock()
        with self._lock:
            if state == "start":
                self._run = self._new_run(now)
            elif isinstance(result, dict) and "points" in result:
                # With no "start" seen, ALP came up part way through a run.
                if self._run is None or self._run["finished_at"] is not None:
                    self._run = self._new_run(now)
                self._run["points"] = _clean_points(result["points"])
                if isinstance(result.get("curve"), list):
                    self._run["curve"] = result["curve"]
                self._run["seen_at"] = now
            elif state in _FINISHED and self._run is not None:
                run = self._run
                # The scope reports "complete" twice, and stopping a view can
                # report "cancel" later on, so the first ending is the real one.
                if run["finished_at"] is None:
                    run["state"] = state
                    run["finished_at"] = now
                    run["error"] = event.get("error")
                if event.get("lapse_ms"):
                    run["lapse_ms"] = event["lapse_ms"]

    def current(self):
        """The run worth showing right now, or None."""
        now = self._clock()
        with self._lock:
            run = self._run
            if run is None:
                return None
            if run["finished_at"] is not None:
                fresh = now - run["finished_at"] <= LINGER_S
            else:
                fresh = now - run["seen_at"] <= RUN_TIMEOUT_S
            return dict(run) if fresh else None


_logs = {}
_logs_lock = threading.Lock()


def focus_log(telescope_id) -> FocusLog:
    with _logs_lock:
        return _logs.setdefault(telescope_id, FocusLog())


def listen():
    """Record every configured scope's events, whether or not a page is open.

    A signal can be connected to before its device exists, and connecting the
    same method twice has no further effect.
    """
    for seestar in Config.seestars:
        log = focus_log(int(seestar["device_num"]))
        signal(f"{seestar['name']}.eventbus").connect(log.on_event, weak=False)


def _ticks(low, high, count=5) -> list:
    """Round values at a clean spacing that cover low to high."""
    rough = (high - low) / max(count - 1, 1) or 1
    power = 10 ** math.floor(math.log10(rough))
    step = next((m * power for m in (1, 2, 5, 10) if m * power >= rough), 10 * power)
    first = math.floor(low / step + 1e-9)
    last = math.ceil(high / step - 1e-9)
    return [round(i * step, 6) for i in range(first, last + 1)]


def _fit_is_usable(curve) -> bool:
    return (
        isinstance(curve, list)
        and len(curve) == 3
        and all(isinstance(c, (int, float)) for c in curve)
        and curve[0] > 0
    )


def _summary(run, final, readings) -> str:
    if run["state"] == "fail":
        return "Autofocus failed" + (f": {run['error']}" if run["error"] else "")
    if run["state"] == "cancel":
        return "Autofocus cancelled"
    if run["state"] == "complete":
        if final is None:
            return "Autofocus finished"
        took = f", took {round(run['lapse_ms'] / 1000)} s" if run["lapse_ms"] else ""
        return f"Focus settled at step {final[0]:g}, metric {final[1]:.2f}{took}"
    if readings:
        return f"Measuring: step {readings[-1][0]:g}, metric {readings[-1][1]:.2f}"
    return "Starting autofocus"


def chart(run) -> dict:
    """Turn a run into what the page draws, in SVG coordinates."""
    points = list(run["points"])
    curve = run["curve"]
    # The scope adds the settled position as a last point when it fits the curve.
    final = None
    if curve is not None and points:
        candidate = points.pop()
        final = candidate if candidate[1] > 0 else None
    readings = [p for p in points if p[1] > 0]
    missing = [p[0] for p in points if p[1] <= 0]

    notes = []
    if missing:
        plural = "step" if len(missing) == 1 else "steps"
        notes.append({"text": f"{len(missing)} {plural} gave no reading", "problem": True})
    fit_ok = _fit_is_usable(curve)
    if curve is not None and not fit_ok:
        notes.append({"text": "The scope reported no usable curve fit", "problem": True})

    drawn = {
        "width": WIDTH,
        "height": HEIGHT,
        "left": LEFT,
        "right": WIDTH - RIGHT,
        "top": TOP,
        "bottom": HEIGHT - BOTTOM,
        "summary": _summary(run, final, readings),
        "state": run["state"],
        "notes": notes,
        "readings": [],
        "missing": [],
        "lines": [],
        "fit": [],
        "final": None,
        "x_ticks": [],
        "y_ticks": [],
        "table": [{"step": s, "metric": m} for s, m in points + ([final] if final else [])],
    }
    if not points and not final:
        return drawn

    steps = [p[0] for p in points] + ([final[0]] if final else [])
    low_step, high_step = min(steps) - 5, max(steps) + 5
    metrics = [p[1] for p in readings] + ([final[1]] if final else [])
    y_ticks = _ticks(0, max(metrics, default=1))
    x_ticks = [t for t in _ticks(low_step, high_step, 6) if low_step <= t <= high_step]
    plot_w = WIDTH - LEFT - RIGHT
    plot_h = HEIGHT - TOP - BOTTOM

    def x_of(step):
        return round(LEFT + (step - low_step) / (high_step - low_step) * plot_w, 1)

    def y_of(metric):
        return round(HEIGHT - BOTTOM - metric / y_ticks[-1] * plot_h, 1)

    drawn["x_ticks"] = [{"x": x_of(t), "label": f"{t:g}"} for t in x_ticks]
    drawn["y_ticks"] = [{"y": y_of(t), "label": f"{t:g}"} for t in y_ticks]
    drawn["readings"] = [
        {"x": x_of(s), "y": y_of(m), "step": s, "metric": round(m, 2)} for s, m in readings
    ]
    drawn["missing"] = [{"x": x_of(s), "step": s} for s in missing]

    # Lines join neighbouring readings only, so a missing one leaves a gap.
    line = []
    for step, metric in points:
        if metric > 0:
            line.append(f"{x_of(step)},{y_of(metric)}")
        else:
            if len(line) > 1:
                drawn["lines"].append(" ".join(line))
            line = []
    if len(line) > 1:
        drawn["lines"].append(" ".join(line))

    if fit_ok:
        segment = []
        for index in range(61):
            step = low_step + (high_step - low_step) * index / 60
            value = curve[0] * step * step + curve[1] * step + curve[2]
            if 0 <= value <= y_ticks[-1]:
                segment.append(f"{x_of(step)},{y_of(value)}")
            else:
                if len(segment) > 1:
                    drawn["fit"].append(" ".join(segment))
                segment = []
        if len(segment) > 1:
            drawn["fit"].append(" ".join(segment))

    if final:
        drawn["final"] = {
            "x": x_of(final[0]),
            "y": y_of(final[1]),
            "step": final[0],
            "metric": round(final[1], 2),
        }
    return drawn
