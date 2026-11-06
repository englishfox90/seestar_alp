"""The roof-gated imaging loop.

One background thread owns every scope command, including those from the page's
buttons, so nothing races. It works in ticks and never blocks through a start,
so a roof change is noticed while the arm is still opening.

  roof safe + dark           -> open the arm (no alignment), run the selected schedule
  roof unsafe, or dawn       -> stop + park
  roof safe again, same night -> resume at the interrupted item
  new night                  -> start the schedule from item 1
  schedule finished          -> park until the next night
  Stop + Park button         -> park and hold until Start or the next night
  session started elsewhere  -> leave it alone, but still park if the roof is unsafe
"""

import collections
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from roof import config as roof_config
from roof import alerts as roof_alerts
from roof import focus, planner, schedule_tools
from roof import sentinel
from roof.queue import FramingEstimates, TargetQueue
from roof.dew import DewMonitor
from roof.night_history import NightHistory
from roof.scope import DryRunScope, RealScope, ScopeState
from roof.sessions import SessionLog
from roof.sun import night_key, sun_altitude_deg

IDLE, OPENING, IMAGING, PARKING = "idle", "opening", "imaging", "parking"

OPEN_TIMEOUT_S = 180
ARM_OPEN_ALARM_S = 300
START_GRACE_S = 15  # the scheduler thread needs a moment to report "working"
THREAD_WIND_DOWN_S = 30
PARK_VERIFY_S = 60
PARK_ATTEMPTS = 3
STOP_RETRY_S = 60
START_RETRY_S = 120
MAX_START_FAILURES = 3
# A plan from the target list that ends well before its time means targets failed,
# and replanning would pick the same ones. This also caps open/park cycles a night.
EARLY_END_FRACTION = 0.5
MAX_LIST_RUNS = 8
HEARTBEAT_S = 1800
LOOP_STALE_S = 90
IDLE_VIEW_STAGES = {"", "None", "ContinuousExposure"}


class RoofAutomation:
    def __init__(
        self,
        load_settings=roof_config.load,
        read_safety=None,
        sun_altitude=sun_altitude_deg,
        scope_factory=None,
        clock=time.time,
        state_path=None,
        schedule_dir=None,
        sleep_wait=None,
        logger=None,
        target_altitude=None,
        dew_monitor=None,
        session_log=None,
        plan_targets=None,
        framing_estimates=None,
        night_history=None,
        alerts=None,
    ):
        self._load_settings = load_settings
        self._read_safety = read_safety or (
            lambda s: sentinel.read(s.sentinel_url, s.safety_device)
        )
        self._sun_altitude = sun_altitude
        self._target_altitude = target_altitude or _default_target_altitude
        self._dew_monitor = dew_monitor or DewMonitor(clock=clock)
        self._dew = None
        self._heater_on = None  # None = unknown; ALP's start-up turns it off
        self._active_items = []
        self._skip_asked = 0
        self._framing_checked = 0
        self._target_rows = []
        self._target_rows_at = 0.0
        self._scope_factory = scope_factory or _default_scopes
        self._scopes = {}
        self._clock = clock
        self._state_path = state_path
        self._schedule_dir = schedule_dir
        self._sleep_wait = sleep_wait or time.sleep
        self._logger = logger or logging.getLogger("roof")

        self._lock = threading.Lock()
        self._commands = collections.deque()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._log_lines = collections.deque(maxlen=200)
        self._snapshot = {
            "safe": False,
            "safety_detail": "not read yet",
            "sun_deg": None,
            "dark": False,
            "connected": False,
            "parked": None,
            "phase": IDLE,
        }

        self.phase = IDLE
        self._dry_run = None
        self._adopted = False
        self._override_roof = False
        self._safe_since = None
        self._unsafe_since = None
        self._was_safe = None
        self._was_dark = None
        self._last_item = 0
        self._active_schedule = ""
        self._deadline = 0.0
        self._arm_deadline = 0.0
        self._park_attempts = 0
        self._last_stop_attempt = 0.0
        self._last_start_attempt = 0.0
        self._start_failures = 0
        self._failure_night = ""
        self._start_item = 1
        self._alarm = ""
        self._loop_error = ""
        self._note = ""
        self._last_heartbeat = 0.0
        self._last_tick = 0.0
        self._started_at = clock()
        self._session_log = session_log
        self._night_history = night_history
        self._alerts = alerts
        self._plan_targets = plan_targets or (lambda s, now: (False, "no target list"))
        self._framing_estimates = framing_estimates
        self._list_note = ""
        self._alarm_after_park = ""
        self._list_runs = 0
        self._list_runs_night = ""
        self._launched_at = 0.0
        self._last_connected_at = None  # only ever reported

        # Survives an ALP restart: a held or finished night must stay that way.
        self._hold_night = ""
        self._completed_night = ""
        self._resume = {}
        self._load_state()

    # ---- public API; safe from any thread ----------------------------------

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="RoofAutomation")
            self._thread.start()

    def shutdown(self):
        self._stop.set()
        self._wake.set()

    def request(self, command: str):
        """Queue 'start', 'stop', or 'schedule_changed' for the loop thread."""
        self._commands.append(command)
        self._wake.set()

    def status(self) -> dict:
        with self._lock:
            snap = dict(self._snapshot)
            snap["log"] = list(self._log_lines)
        age = self._clock() - self._last_tick if self._last_tick else None
        snap["loop_age_s"] = age
        snap["loop_stale"] = age is None or age > LOOP_STALE_S
        return snap

    # ---- loop -----------------------------------------------------------------

    def _run(self):
        self._log("automation thread started")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                self._logger.exception("roof tick failed")
                self._loop_error = f"loop error: {e}"
                self._alert("alarm", f"automation {self._loop_error}")
            else:
                self._loop_error = ""
            try:
                poll = self._load_settings().poll_s
            except Exception:
                poll = 10.0
            self._wake.wait(poll)
            self._wake.clear()

    def tick(self):
        s = self._load_settings()
        now = self._clock()
        scope = self._select_scope(s)
        safety = self._read_safety(s)
        sun = self._read_sun(s, now)
        dark = sun is not None and sun < s.sun_max_deg
        night = night_key(now, s.lon)
        self._track_conditions(safety, dark, sun, now, s)

        state = scope.read()
        self._note = ""
        command = self._next_commands(scope, s, state, safety, dark, night, now)
        if not command:
            state = self._step(scope, s, state, safety, dark, night, now)
        self._manage_dew(scope, s, state)
        self._record_stack(state, night, now)
        self._alert(
            "observe",
            state,
            dark,
            night,
            self.phase,
            self._note,
            self._target_position(state),
            s.dry_run,
        )
        self._publish(s, safety, sun, dark, night, state, now)
        self._last_tick = now
        if now - self._last_heartbeat >= HEARTBEAT_S:
            self._last_heartbeat = now
            self._log(
                f"alive: roof={'SAFE' if safety.safe else 'UNSAFE'} sun="
                f"{'?' if sun is None else f'{sun:.1f}'} phase={self.phase} "
                f"enabled={s.enabled}"
            )

    def _select_scope(self, s):
        if self._dry_run is not None and self._dry_run != s.dry_run:
            self._log(f"dry_run changed to {s.dry_run}; forgetting the current session")
            self.phase = IDLE
            self._adopted = False
            self._alert("session_forgotten")
            self._scopes.pop(True, None)  # a later dry run starts from the real state
        self._dry_run = s.dry_run
        if s.dry_run not in self._scopes:
            self._scopes[s.dry_run] = self._scope_factory(s)
        return self._scopes[s.dry_run]

    def _read_sun(self, s, now):
        if s.lat == 0 and s.lon == 0:
            return None  # no site configured, so it can never count as dark
        return self._sun_altitude(s.lat, s.lon, datetime.fromtimestamp(now, timezone.utc))

    def _track_conditions(self, safety, dark, sun, now, s):
        if safety.safe:
            self._unsafe_since = None
            if self._safe_since is None:
                self._safe_since = now
            if self._override_roof:
                self._override_roof = False
                self._log("roof reads safe; manual roof override ended")
        else:
            self._safe_since = None
            if self._unsafe_since is None:
                self._unsafe_since = now
        if safety.safe != self._was_safe:
            self._log(f"roof {'SAFE' if safety.safe else 'UNSAFE'} ({safety.detail})")
            self._was_safe = safety.safe
            self._record_roof(safety, now, s)
        if dark != self._was_dark:
            sun_text = "unknown" if sun is None else f"{sun:.1f} deg"
            self._log(f"{'dark' if dark else 'not dark'} (sun {sun_text}, limit {s.sun_max_deg})")
            self._was_dark = dark

    def _next_commands(self, scope, s, state, safety, dark, night, now) -> bool:
        """Run queued button presses. True when one took this tick's action."""
        acted = False
        while self._commands:
            command = self._commands.popleft()
            if command == "schedule_changed":
                self._completed_night = ""
                self._resume = {}
                self._target_rows_at = 0.0
                self._save_state()
                self._log(f"schedule set to '{s.schedule}'; applies at the next start")
            elif command == "stop":
                self._manual_stop(scope, state, night, now)
                acted = True
            elif command == "start":
                self._manual_start(scope, s, state, safety, dark, night, now)
                acted = True
        return acted

    def _step(self, scope, s, state, safety, dark, night, now):
        reason = self._stop_reason(s, state, safety, dark, now) if s.enabled else None
        if reason and state.connected:
            self._begin_stop(scope, reason, state, night, now)
        elif reason:
            message = f"should stop + park ({reason}) but the scope is not connected"
            if self._alarm != message:
                self._raise_alarm(message)
        elif self.phase == PARKING:
            self._advance_parking(scope, state, now)
        elif self.phase == OPENING:
            self._advance_opening(scope, s, state, night, now)
        elif self.phase == IMAGING:
            self._advance_imaging(scope, s, state, night, now)
        else:
            self._consider_start(scope, s, state, safety, dark, night, now)
        return state

    # ---- stopping ---------------------------------------------------------------

    def _stop_reason(self, s, state, safety, dark, now):
        if self.phase == PARKING:
            return None
        if now - self._last_stop_attempt < STOP_RETRY_S:
            return None
        session = self.phase in (OPENING, IMAGING) or state.sched_state == "working"
        if (
            not safety.safe
            and not self._override_roof
            and self._unsafe_since is not None
            and now - self._unsafe_since >= s.stop_grace_s
            and (state.parked is not True or session)
        ):
            return "roof unsafe"
        if not dark and self.phase in (OPENING, IMAGING):
            return "dawn"
        return None

    def _begin_stop(self, scope, reason, state, night, now, keep_resume=True):
        if (
            keep_resume
            and self.phase == IMAGING
            and not self._adopted
            and self._last_item
            and self._active_schedule != schedule_tools.TARGET_LIST  # replans instead
        ):
            self._resume = {
                "schedule": self._active_schedule,
                "item": self._last_item,
                "night": night,
            }
            self._log(f"will resume at item {self._last_item} if this night continues")
            self._save_state()
        self._log(f"STOP + PARK ({reason})")
        self._alert("session_ended", reason)
        self._last_stop_attempt = now
        self._adopted = False
        self._override_roof = False

        def step(name, fn):
            self._log(f"  {name}")
            try:
                failure = fn()
            except Exception as e:
                failure = str(e)
            if failure:
                self._log(f"  {name}: {failure}")
            return failure

        # Order matters: an app ViewPlan overrides stop_view and scope_park.
        step("take master", scope.ensure_master)
        step("clear_view_plan", scope.clear_view_plan)
        step("stop_scheduler", scope.stop_scheduler)
        self._wait_for_threads(scope)
        step("iscope_stop_view", scope.stop_view)
        step("scope_park", scope.park)
        self.phase = PARKING
        self._park_attempts = 1
        self._deadline = now + PARK_VERIFY_S

    def _wait_for_threads(self, scope):
        # An item or start-up thread winding down could otherwise send a command
        # after the park. Waited for, but never allowed to block the park.
        waited = 0
        while waited < THREAD_WIND_DOWN_S and scope.busy():
            self._sleep_wait(1)
            waited += 1
        if waited >= THREAD_WIND_DOWN_S:
            self._log("  scope threads still running; parking anyway")

    def _advance_parking(self, scope, state, now):
        if state.parked is True:
            self.phase = IDLE
            # Parking clears alarms about parking, but not one that caused the park.
            self._alarm, self._alarm_after_park = self._alarm_after_park, ""
            self._log("parked: firmware reports mount closed")
            self._alert("parked")
            return
        if now < self._deadline:
            return
        if self._park_attempts < PARK_ATTEMPTS:
            self._park_attempts += 1
            self._deadline = now + PARK_VERIFY_S
            self._log(f"not parked yet; scope_park attempt {self._park_attempts}")
            try:
                scope.park()
            except Exception as e:
                self._log(f"  scope_park: {e}")
            return
        self.phase = IDLE
        self._last_stop_attempt = now
        self._raise_alarm(f"PARK NOT CONFIRMED after {PARK_ATTEMPTS} attempts (mount.close={state.parked})")

    def _manual_stop(self, scope, state, night, now):
        self._hold_night = night
        self._save_state()
        self._log("Stop + Park pressed; holding until Start or the next night")
        if state.connected:
            self._begin_stop(scope, "manual", state, night, now)
        else:
            self._raise_alarm("Stop + Park pressed but the scope is not connected")

    # ---- starting ---------------------------------------------------------------

    def _manual_start(self, scope, s, state, safety, dark, night, now):
        if not dark:
            self._log("Start refused: it is not dark")
            return
        if not state.connected:
            self._log("Start refused: the scope is not connected")
            return
        if state.sched_state == "working" or self._foreign(state):
            self._log("Start refused: a session is already running")
            return
        if self.phase in (OPENING, IMAGING, PARKING):
            self._log(f"Start refused: automation is {self.phase}")
            return
        if self._completed_night == night:
            self._completed_night = ""
            self._resume = {}
        if self._first_item_to_run(s, night, now) is None:
            self._save_state()
            self._log(f"Start refused: {self._no_target_reason(s)}")
            return
        self._hold_night = ""
        self._save_state()
        self._start_failures = 0
        self._alarm = ""
        if not safety.safe:
            self._override_roof = True
            self._log("Start pressed with the roof NOT safe; roof ignored until it next reads safe")
        else:
            self._log("Start pressed")
        self._begin_start(scope, s, state, night, now)

    def _consider_start(self, scope, s, state, safety, dark, night, now):
        if state.sched_state == "working":
            self.phase = IMAGING
            self._adopted = True
            self._arm_deadline = 0.0
            self._last_item = 0
            self._deadline = now
            self._log("a scheduler started elsewhere is running; watching it")
            self._alert("session_adopted", night)
            return
        if not s.enabled:
            self._note = "automation disabled"
            return
        blocked = self._start_block(s, state, safety, dark, night, now)
        if blocked:
            self._note = blocked
            return
        if self._first_item_to_run(s, night, now) is None:
            self._note = f"waiting: {self._no_target_reason(s)}"
            return
        self._last_start_attempt = now
        self._log(f"roof safe and dark; starting '{schedule_tools.label(s.schedule)}'")
        self._begin_start(scope, s, state, night, now)

    def _start_block(self, s, state, safety, dark, night, now):
        if not state.connected:
            return "waiting: scope not connected"
        if not safety.safe:
            return "waiting: roof not safe"
        if now - (self._safe_since or now) < s.start_settle_s:
            return f"waiting: roof must read safe for {s.start_settle_s:.0f}s"
        if not dark:
            return "waiting: not dark"
        if self._hold_night == night:
            return "held after Stop + Park until Start or the next night"
        if self._completed_night == night:
            return "schedule finished tonight"
        if not s.schedule:
            return "no schedule selected"
        if self._foreign(state):
            return "another session is running; leaving it alone"
        if state.sched_state not in ("stopped", "complete"):
            return f"waiting: scheduler is {state.sched_state}"
        if self._failure_night == night and self._start_failures >= MAX_START_FAILURES:
            return "start failed repeatedly; press Start to retry"
        if now - self._last_start_attempt < START_RETRY_S and self._failure_night == night:
            return "waiting to retry the start"
        return None

    @staticmethod
    def _foreign(state: ScopeState) -> bool:
        if state.sched_state == "working":
            return False
        if state.plan_state == "working":
            return True
        return state.view_state == "working" and state.view_stage not in IDLE_VIEW_STAGES

    def _begin_start(self, scope, s, state, night, now):
        path = self._schedule_path(s)
        if not path:
            self._start_failed(f"schedule '{schedule_tools.label(s.schedule)}' not found", night)
            return
        _, errors, warnings = schedule_tools.load(path)
        for warning in warnings:
            self._log(f"schedule warning: {warning}")
        if errors:
            self._start_failed("schedule rejected: " + "; ".join(errors), night)
            return
        self._start_item = self._first_item_to_run(s, night, now) or 1
        if state.parked is None:
            self._start_failed("cannot read mount state", night)
        elif state.parked:
            self._log(f"opening arm: start_up_sequence 3ppa=false lat={s.lat} lon={s.lon}")
            failure = self._guard(scope.open_arm, s.lat, s.lon)
            if failure:
                self._start_failed(f"start_up_sequence: {failure}", night)
                return
            self.phase = OPENING
            self._heater_on = None  # start-up resends the heater from config.toml
            self._deadline = now + OPEN_TIMEOUT_S
        else:
            self._launch(scope, s, night, now)

    def _first_item_to_run(self, s, night, now):
        """Item to start from: the resume point or item 1, moved past any target
        below the altitude limit. None if no target from there on is above it.
        The target list plans afresh instead, so it has no resume point."""
        if s.schedule == schedule_tools.TARGET_LIST:
            return 1 if self._refresh_list_plan(s, now) else None
        resume = self._resume
        start = 1
        if resume.get("night") == night and resume.get("schedule") == s.schedule:
            start = int(resume.get("item") or 1)
        path = self._schedule_path(s)
        items, errors, _ = schedule_tools.load(path) if path else ([], ["missing"], [])
        if errors:
            return start  # _begin_start reports it
        if start > len(items):
            start = 1
        if s.min_target_alt_deg <= 0:
            return start
        for number in range(start, len(items) + 1):
            item = items[number - 1]
            if item.get("action") in schedule_tools.IMAGING_ACTIONS and self._high_enough(
                item, s, now
            ):
                return number
        return None

    def _schedule_path(self, s) -> str | None:
        name = schedule_tools.PLAN_FILE if s.schedule == schedule_tools.TARGET_LIST else s.schedule
        return schedule_tools.resolve(name, self._schedule_dir) if name else None

    def _refresh_list_plan(self, s, now) -> bool:
        """Write tonight's plan from the target list. True if a target is ready now."""
        try:
            ready, self._list_note = self._plan_targets(s, now)
        except Exception as e:
            self._logger.warning("could not plan from the target list: %s", e)
            self._list_note = f"cannot plan from the target list: {e}"
            return False
        return ready

    def _no_target_reason(self, s) -> str:
        if s.schedule == schedule_tools.TARGET_LIST:
            return self._list_note
        return f"no remaining target is above {s.min_target_alt_deg:g} deg"

    def _altitude(self, item, s, now):
        """Target altitude, or None when it has no fixed position."""
        return self._target_altitude(
            item.get("params", {}), s.lat, s.lon, datetime.fromtimestamp(now, timezone.utc)
        )

    def _high_enough(self, item, s, now) -> bool:
        try:
            altitude = self._altitude(item, s, now)
        except Exception as e:
            self._log(f"cannot compute altitude: {e}")
            return False
        return altitude is None or altitude >= s.min_target_alt_deg

    def _advance_opening(self, scope, s, state, night, now):
        # Start-up ends with state "complete", or "stopped" if it failed. mount.close
        # stays true until the schedule's first goto raises the arm (seen on hardware
        # 2026-11-05), so an open arm cannot be required here.
        if state.busy or state.sched_state == "working":
            if now >= self._deadline:
                self.phase = IDLE
                self._start_failed(f"start-up not finished after {OPEN_TIMEOUT_S}s", night)
            return
        if state.sched_state == "complete":
            self._launch(scope, s, night, now)
        else:
            self.phase = IDLE
            self._start_failed(f"start-up ended with state {state.sched_state}", night)

    def _launch(self, scope, s, night, now):
        self.phase = IDLE
        path = self._schedule_path(s)
        items, errors, _ = schedule_tools.load(path) if path else ([], ["schedule not found"], [])
        if errors:
            self._start_failed("; ".join(errors), night)
            return
        start_item = self._start_item if self._start_item <= len(items) else 1
        self._log(f"import_schedule {os.path.basename(path)}")
        failure = self._guard(scope.import_schedule, path)
        if failure:
            self._start_failed(f"import_schedule: {failure}", night)
            return
        for item in items[: start_item - 1]:
            if item.get("action") in schedule_tools.SETTING_ACTIONS:
                self._log(f"re-applying {item['action']} before resuming")
                failure = self._guard(scope.apply_setting, item)
                if failure:
                    self._log(f"  {item['action']}: {failure}")
        self._log(f"start_scheduler from item {start_item} of {len(items)}")
        failure = self._guard(scope.start_scheduler, start_item)
        if failure:
            self._start_failed(f"start_scheduler: {failure}", night)
            return
        self.phase = IMAGING
        self._adopted = False
        self._active_schedule = s.schedule
        self._active_items = items
        self._launched_at = now
        self._alarm_after_park = ""
        self._skip_asked = 0
        self._framing_checked = 0
        self._last_item = start_item
        self._deadline = now + START_GRACE_S
        self._arm_deadline = now + ARM_OPEN_ALARM_S
        self._start_failures = 0
        self._alarm = ""
        self._alert(
            "session_started",
            schedule_tools.label(s.schedule),
            *self._targets_from(items, start_item),
            self._resume.get("night") == night,
            night,
        )
        if self._resume:
            self._resume = {}
            self._save_state()

    @staticmethod
    def _targets_from(items, start_item):
        """(names of the targets still to come, position of the first among all)."""
        targets = [
            (number, item.get("params", {}).get("target_name", "?"))
            for number, item in enumerate(items, 1)
            if item.get("action") in schedule_tools.IMAGING_ACTIONS
        ]
        ahead = [name for number, name in targets if number >= start_item]
        return ahead, len(targets) - len(ahead) + 1

    @staticmethod
    def _guard(fn, *args):
        try:
            return fn(*args)
        except Exception as e:
            return str(e)

    def _start_failed(self, why, night):
        if self._failure_night != night:
            self._start_failures = 0
        self._failure_night = night
        self._start_failures += 1
        self._last_start_attempt = self._clock()
        self.phase = IDLE
        self._raise_alarm(f"start failed ({self._start_failures}/{MAX_START_FAILURES}): {why}")

    # ---- imaging ----------------------------------------------------------------

    def _advance_imaging(self, scope, s, state, night, now):
        if state.sched_state == "working":
            if state.item_number > 0:
                self._last_item = state.item_number
            if state.parked is True and self._arm_deadline and now >= self._arm_deadline:
                self._arm_deadline = 0.0
                self._raise_alarm(f"arm still closed {ARM_OPEN_ALARM_S}s after the schedule started")
            elif state.parked is False:
                self._arm_deadline = 0.0
            self._skip_if_too_low(scope, s, state, now)
            self._check_framing(state)
            return
        if state.sched_state in ("stopping", "unknown"):
            return
        if state.sched_state == "complete":
            adopted = self._adopted
            self._log("schedule finished")
            self.phase = IDLE
            self._adopted = False
            if adopted:
                self._alert("session_ended", "schedule finished")
                return
            if not self._list_run_may_replan(night, now):
                self._completed_night = night
            self._resume = {}
            self._save_state()
            self._alert("session_ended", "schedule finished", self._completed_night != night)
            if s.enabled and state.connected:
                self._begin_stop(scope, "schedule finished", state, night, now, keep_resume=False)
            return
        if now < self._deadline:
            return  # still inside the grace period after start_scheduler
        self.phase = IDLE
        self._adopted = False
        self._hold_night = night
        self._save_state()
        self._alert("session_ended", "stopped elsewhere")
        self._raise_alarm("scheduler stopped outside automation; holding until Start or the next night")

    def _list_run_may_replan(self, night, now) -> bool:
        """After a plan from the target list ends, may the night carry on?"""
        if self._active_schedule != schedule_tools.TARGET_LIST:
            return False
        planned = sum(
            item.get("params", {}).get("panel_time_sec", 0)
            for item in self._active_items
            if item.get("action") in schedule_tools.IMAGING_ACTIONS
        )
        if now - self._launched_at < planned * EARLY_END_FRACTION:
            self._alarm_after_park = (
                "the target list's plan ended well before its time, so a target probably "
                "failed; done for tonight. Press Start to try again"
            )
            self._raise_alarm(self._alarm_after_park)
            return False
        if self._list_runs_night != night:
            self._list_runs_night, self._list_runs = night, 0
        self._list_runs += 1
        if self._list_runs >= MAX_LIST_RUNS:
            self._log(f"stopping for tonight after {MAX_LIST_RUNS} runs from the target list")
            return False
        return True

    def _skip_if_too_low(self, scope, s, state, now):
        number = state.item_number
        if (
            s.min_target_alt_deg <= 0
            or self._adopted
            or not 1 <= number <= len(self._active_items)
            or number == self._skip_asked
        ):
            return
        item = self._active_items[number - 1]
        if item.get("action") not in schedule_tools.IMAGING_ACTIONS:
            return
        if self._high_enough(item, s, now):
            return
        name = item.get("params", {}).get("target_name", "?")
        self._log(f"skipping item {number} ({name}): below {s.min_target_alt_deg:g} deg")
        self._skip_asked = number
        self._alert("target_skipped", name, f"below {s.min_target_alt_deg:g} deg")
        failure = self._guard(scope.skip_item)
        if failure:
            self._log(f"  skip: {failure}")

    def _check_framing(self, state):
        """Alarm if a framed mosaic is stacking without its mosaic scale."""
        number = state.item_number
        if (
            self._adopted
            or state.view_stage != "Stack"
            or not 1 <= number <= len(self._active_items)
            or state.mosaic_scale is None
            or self._framing_checked == number
        ):
            return
        self._framing_checked = number
        item = self._active_items[number - 1]
        wanted = item.get("params", {}).get("mosaic_scale")
        if item.get("action") != "start_framed_mosaic" or not wanted:
            return
        if abs(state.mosaic_scale - float(wanted)) > 0.05:
            name = item["params"].get("target_name", "?")
            self._raise_alarm(
                f"{name} is stacking at scale {state.mosaic_scale:g}, not the planned {wanted:g}"
            )
        else:
            self._log(f"framing confirmed: mosaic scale {state.mosaic_scale:g}")

    def _manage_dew(self, scope, s, state):
        """Heater follows the dew margin during our sessions, and is off otherwise.

        With no weather data it stays on: a wet lens costs more than a little heat.
        """
        if not s.dew_enabled:
            return
        self._dew = self._dew_monitor.read(s)
        if not state.connected:
            return
        running = self.phase == IMAGING and not self._adopted
        if not running:
            # Unknown counts too: a restart can leave it on from the last session.
            if self.phase == IDLE and self._heater_on is not False and state.parked is True:
                self._set_heater(scope, False, s, "parked")
            return
        if self._dew is None:
            if self._heater_on is not True:
                self._set_heater(scope, True, s, "no weather data")
        elif self._dew.margin_c <= s.dew_on_margin_c:
            if self._heater_on is not True:
                self._set_heater(scope, True, s, self._dew_text())
        elif self._dew.margin_c >= s.dew_off_margin_c or self._heater_on is None:
            if self._heater_on is not False:
                self._set_heater(scope, False, s, self._dew_text())

    def _dew_text(self):
        d = self._dew
        return f"{d.temp_c:.1f}C, dew point {d.dew_point_c:.1f}C, margin {d.margin_c:.1f}C ({d.source})"

    def _set_heater(self, scope, on, s, why):
        power = s.dew_power if on else 0
        self._log(f"dew heater {'on at ' + str(power) + '%' if on else 'off'}: {why}")
        failure = self._guard(scope.set_heater, power)
        if failure:
            self._log(f"  dew heater: {failure}")
        else:
            self._heater_on = on

    def _alert(self, method, *args):
        """Bookkeeping only: a failure here must never stop the safety loop."""
        if self._alerts is None:
            return
        try:
            getattr(self._alerts, method)(*args)
        except Exception as e:
            self._logger.warning("could not raise the %s alert: %s", method, e)

    def _record_roof(self, safety, now, s):
        """Bookkeeping only: a failure here must never stop the safety loop."""
        if self._night_history is None:
            return
        try:
            self._night_history.record(safety, night_key(now, s.lon), now)
        except Exception as e:
            self._logger.warning("could not record the roof history: %s", e)

    def _record_stack(self, state, night, now):
        """Bookkeeping only: a failure here must never stop the safety loop."""
        if self._session_log is None:
            return
        try:
            self._session_log.observe(state, night, now)
            self._learn_framing_estimate(state)
        except Exception as e:
            self._logger.warning("could not record the stack: %s", e)

    def _learn_framing_estimate(self, state):
        # Only while stacking, when the firmware's setting is this target's framing.
        if (
            self._framing_estimates is None
            or state.view_stage != "Stack"
            or (state.mosaic_scale or 0) <= 1.0
            or not state.mosaic_estimate_h
        ):
            return
        if self._framing_estimates.note(state.mosaic_scale, state.mosaic_estimate_h):
            self._log(
                f"scope estimates {state.mosaic_estimate_h:g} h for a "
                f"{state.mosaic_scale:g}x framing"
            )

    def _targets_now(self, s, now):
        """Altitudes for the page, refreshed once a minute."""
        if now - self._target_rows_at < 60 and self._target_rows_at:
            return self._target_rows
        self._target_rows_at = now
        rows = []
        path = self._schedule_path(s)
        items = schedule_tools.load(path)[0] if path else []
        for item in items:
            if item.get("action") not in schedule_tools.IMAGING_ACTIONS:
                continue
            try:
                altitude = self._altitude(item, s, now)
            except Exception:
                altitude = None
            rows.append(
                {
                    "name": item.get("params", {}).get("target_name", "?"),
                    "kind": schedule_tools.describe_target(item),
                    "altitude": altitude,
                    "ok": altitude is None or altitude >= s.min_target_alt_deg,
                }
            )
        self._target_rows = rows
        return rows

    # ---- persistence, logging, status -------------------------------------------

    def _load_state(self):
        if not self._state_path:
            return
        try:
            with open(self._state_path) as f:
                doc = json.load(f)
            self._hold_night = str(doc.get("hold_night", ""))
            self._completed_night = str(doc.get("completed_night", ""))
            resume = doc.get("resume")
            self._resume = resume if isinstance(resume, dict) else {}
        except (OSError, ValueError):
            pass

    def _save_state(self):
        if not self._state_path:
            return
        tmp = f"{self._state_path}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(
                    {
                        "hold_night": self._hold_night,
                        "completed_night": self._completed_night,
                        "resume": self._resume,
                    },
                    f,
                )
            os.replace(tmp, self._state_path)
        except OSError as e:
            self._logger.warning("could not save roof state: %s", e)

    def _raise_alarm(self, message):
        self._alarm = message
        self._log(f"ALARM: {message}")
        self._alert("alarm", message)

    def _log(self, message):
        dry = "[DRY RUN] " if self._dry_run is True else ""
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {dry}{message}"
        with self._lock:
            self._log_lines.append(line)
        self._logger.info("%s%s", dry, message)

    def _target_position(self, state):
        """(n, total) among imaging targets; schedules also hold settings items."""
        items = self._active_items
        number = state.item_number
        if self._adopted or not 1 <= number <= len(items):
            return 0, 0
        if items[number - 1].get("action") not in schedule_tools.IMAGING_ACTIONS:
            return 0, 0
        is_target = [i.get("action") in schedule_tools.IMAGING_ACTIONS for i in items]
        return sum(is_target[:number]), sum(is_target)

    def _headline(self, state, foreign) -> str:
        """What it's doing right now, in a sentence."""
        if self.phase == OPENING:
            return "Opening the arm"
        if self.phase == PARKING:
            return "Parking"
        if self.phase == IMAGING:
            index, total = self._target_position(state)
            where = f"(target {index} of {total})" if index else ""
            what = " ".join(p for p in (state.target, where) if p)
            return f"Imaging {what}".strip()
        if foreign:
            return "A session started elsewhere is running"
        return (self._note or "Idle").capitalize()

    def _publish(self, s, safety, sun, dark, night, state, now):
        foreign = self._foreign(state)
        if state.connected:
            self._last_connected_at = now
        resume = self._resume if self._resume.get("night") == night else {}
        snapshot = {
            "enabled": s.enabled,
            "dry_run": s.dry_run,
            "schedule": schedule_tools.label(s.schedule),
            "uses_target_list": s.schedule == schedule_tools.TARGET_LIST,
            "safe": safety.safe,
            "safety_detail": safety.detail,
            "sun_deg": sun,
            "dark": dark,
            "sun_max_deg": s.sun_max_deg,
            "min_target_alt_deg": s.min_target_alt_deg,
            "targets": self._targets_now(s, now),
            "phase": self.phase,
            "connected": state.connected,
            "scope_name": state.name,
            "parked": state.parked,
            "tracking": state.tracking,
            "sched_state": state.sched_state,
            "item_number": state.item_number,
            "item_count": state.item_count,
            "target_index": self._target_position(state)[0],
            "target_total": self._target_position(state)[1],
            "target": state.target,
            "view_state": state.view_state,
            "view_stage": state.view_stage,
            "plan_state": state.plan_state,
            "mosaic_scale": state.mosaic_scale,
            # Read here anyway, so a page can show the stack without asking the scope.
            "view_target": state.view_target,
            "stacked": state.stacked,
            "dropped": state.dropped,
            "exposure_s": state.exposure_s,
            "stack_lapse_s": state.stack_lapse_s,
            "gain": state.gain,
            "last_connected_at": self._last_connected_at,
            "foreign": foreign,
            "override_roof": self._override_roof,
            "held": self._hold_night == night,
            "completed": self._completed_night == night,
            "resume_item": resume.get("item") if resume.get("schedule") == s.schedule else None,
            "alarm": self._loop_error or self._alarm,
            "headline": self._headline(state, foreign),
            "dew_enabled": s.dew_enabled,
            "dew_temp_c": self._dew.temp_c if self._dew else None,
            "dew_point_c": self._dew.dew_point_c if self._dew else None,
            "dew_margin_c": self._dew.margin_c if self._dew else None,
            "dew_source": self._dew.source if self._dew else "",
            "dew_on_margin_c": s.dew_on_margin_c,
            "heater_on": self._heater_on,
            "dew_power": s.dew_power,
            "started_at": self._started_at,
            "uptime_s": now - self._started_at,
            "pid": os.getpid(),
            "note": self._note,
        }
        with self._lock:
            self._snapshot = snapshot


_target_queue = None


def _default_target_altitude(params, lat, lon, when):
    from roof.targets import target_altitude_deg

    return target_altitude_deg(params, lat, lon, when)


def _default_scopes(s):
    from device import telescope

    real = RealScope(lambda: telescope.seestar_dev.get(s.device_num))
    return DryRunScope(real) if s.dry_run else real


_instance = None


def target_queue() -> TargetQueue:
    get_automation()
    return _target_queue


def session_log() -> SessionLog | None:
    return get_automation()._session_log


def night_history() -> NightHistory | None:
    return get_automation()._night_history


def get_automation() -> RoofAutomation:
    """The process-wide automation, created on first use."""
    global _instance
    if _instance is None:
        from device.config import Config

        base = os.path.dirname(Config.path_to_dat)
        logger = logging.getLogger("roof")
        if not logger.handlers:
            logger.setLevel(logging.INFO)
            handler = RotatingFileHandler(
                os.path.join(os.getcwd(), "roof_automation.log"),
                maxBytes=2_000_000,
                backupCount=3,
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
        log = SessionLog(os.path.join(base, "roof_sessions.json"))
        targets = TargetQueue(os.path.join(base, "roof_targets.json"))
        global _target_queue
        _target_queue = targets
        webhook = os.path.join(base, roof_alerts.WEBHOOK_FILE)
        alerts = roof_alerts.NightAlerts(
            roof_alerts.DiscordSender(
                lambda: roof_alerts.read_webhook(webhook)
                if roof_config.load().discord_enabled
                else ""
            ),
            state_path=os.path.join(base, "roof_alerts.json"),
            logger=logger,
        )
        focus.focus_log(roof_config.load().device_num).on_finish = alerts.autofocus
        _instance = RoofAutomation(
            state_path=os.path.join(base, "roof_state.json"),
            logger=logger,
            session_log=log,
            night_history=NightHistory(os.path.join(base, "roof_night_history.json")),
            alerts=alerts,
            framing_estimates=FramingEstimates(os.path.join(base, "roof_framing.json")),
            plan_targets=lambda s, now: planner.write_night_plan(
                s, targets.all(), log, now, schedule_tools.schedule_dir()
            ),
        )
    return _instance
