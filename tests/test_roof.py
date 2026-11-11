"""Roof automation: loop decisions, safety reads, sun, schedule rules, page."""

import json
import threading
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from roof import schedule_tools, sentinel
from roof.automation import IDLE, IMAGING, OPENING, PARKING, RoofAutomation
from roof.config import Settings
from roof.scope import DryRunScope, RealScope, ScopeState
from roof.sentinel import SafetyReading
from roof.sun import night_key, sun_altitude_deg

SAFE = SafetyReading(True, True, True, "safe")
UNSAFE = SafetyReading(False, True, False, "IsSafe is not true")
# 2026-11-05 03:00 UTC is dark at the site; the next day's 18:00 UTC is daylight.
NIGHT_START = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc).timestamp()


class FakeScope:
    dry_run = False

    def __init__(self):
        self.state = ScopeState(connected=True, parked=True, sched_state="stopped")
        self.calls = []
        self.fail = {}
        self.busy_flag = False

    def read(self):
        return self.state

    def busy(self):
        return self.busy_flag

    def _record(self, name, *args):
        self.calls.append((name, *args) if args else name)
        return self.fail.get(name)

    def open_arm(self, lat, lon):
        return self._record("open_arm", lat, lon)

    def import_schedule(self, path):
        return self._record("import_schedule")

    def apply_setting(self, item):
        return self._record("apply_setting", item["action"])

    def start_scheduler(self, start_item):
        return self._record("start_scheduler", start_item)

    def ensure_master(self):
        return self._record("ensure_master")

    def clear_view_plan(self):
        return self._record("clear_view_plan")

    def stop_scheduler(self):
        return self._record("stop_scheduler")

    def stop_view(self):
        return self._record("stop_view")

    def skip_item(self):
        return self._record("skip_item")

    def park(self):
        return self._record("park")


class Rig:
    night_history = None

    def __init__(self, tmp_path, **settings):
        self.dir = tmp_path / "schedule"
        self.dir.mkdir(exist_ok=True)
        self.write(
            "Good.json",
            [
                {"action": "set_setting", "params": {"exp_ms": {"stack_l": 30000}}},
                _target("A"),
                _target("B"),
            ],
        )
        self.settings = Settings(
            enabled=True,
            dry_run=False,
            schedule="Good.json",
            lat=31.5,
            lon=-99.37,
            start_settle_s=120,
            stop_grace_s=30,
            **settings,
        )
        self.now = NIGHT_START
        self.safety = SAFE
        self.sun = -40.0
        self.alts = {}
        self.scope = FakeScope()
        self.state_path = str(tmp_path / "state.json")
        self.auto = self.build()

    def build(self):
        return RoofAutomation(
            load_settings=lambda: self.settings,
            read_safety=lambda s: self.safety,
            sun_altitude=lambda lat, lon, when: self.sun,
            scope_factory=lambda s: self.scope,
            clock=lambda: self.now,
            state_path=self.state_path,
            schedule_dir=str(self.dir),
            sleep_wait=lambda s: None,
            target_altitude=lambda params, lat, lon, when: self.alts.get(
                params.get("target_name"), 60.0
            ),
            night_history=self.night_history,
        )

    def write(self, name, items):
        (self.dir / name).write_text(json.dumps({"list": items}))

    def tick(self, advance=0):
        self.now += advance
        self.auto.tick()

    def set_scope(self, **changes):
        self.scope.state = replace(self.scope.state, **changes)

    def settle_and_start(self):
        """Roof safe and dark from a parked scope, through to imaging."""
        self.tick()
        self.tick(121)  # sends open_arm
        assert self.auto.phase == OPENING
        self.set_scope(parked=False, sched_state="complete")
        self.tick(30)  # sends import + start
        assert self.auto.phase == IMAGING
        self.set_scope(sched_state="working", item_number=1, item_count=3)
        self.tick(10)

    def stop_calls(self):
        return [c for c in self.scope.calls if c in ("clear_view_plan", "stop_scheduler", "stop_view", "park")]


def _target(name):
    return {
        "action": "start_mosaic",
        "params": {"target_name": name, "panel_time_sec": 3600},
    }


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_defaults_are_off_and_dry():
    s = Settings()
    assert s.enabled is False and s.dry_run is True


def test_waits_for_settle_then_opens_without_alignment(rig):
    rig.tick()
    assert rig.scope.calls == []
    rig.tick(119)
    assert rig.scope.calls == []
    rig.tick(2)
    assert rig.scope.calls == [("open_arm", 31.5, -99.37)]
    assert rig.auto.phase == OPENING


def test_full_start_order_after_arm_opens(rig):
    rig.settle_and_start()
    assert rig.scope.calls[1:] == ["import_schedule", ("start_scheduler", 1)]


def test_waits_while_startup_thread_runs(rig):
    rig.tick()
    rig.tick(121)
    rig.set_scope(sched_state="working", busy=True)
    rig.tick(10)
    assert "import_schedule" not in rig.scope.calls
    assert rig.auto.phase == OPENING


def test_launches_after_startup_even_though_mount_still_reads_closed(rig):
    # Hardware 2026-11-05: mount.close stays true until the first goto.
    rig.tick()
    rig.tick(121)
    rig.set_scope(sched_state="complete")
    rig.tick(10)
    assert ("start_scheduler", 1) in rig.scope.calls
    rig.set_scope(sched_state="working", item_number=1)
    rig.tick(10)
    assert rig.auto.status()["alarm"] == ""


def test_alarms_if_arm_never_opens_after_launch(rig):
    rig.tick()
    rig.tick(121)
    rig.set_scope(sched_state="complete")
    rig.tick(10)
    rig.set_scope(sched_state="working", item_number=1)
    rig.tick(301)
    assert "arm still closed" in rig.auto.status()["alarm"]


def test_failed_startup_does_not_launch(rig):
    rig.tick()
    rig.tick(121)
    rig.set_scope(sched_state="stopped")
    rig.tick(10)
    assert "import_schedule" not in rig.scope.calls
    assert "start-up ended" in rig.auto.status()["alarm"]


def test_startup_timeout_alarms(rig):
    rig.tick()
    rig.tick(121)
    rig.set_scope(sched_state="working", busy=True)
    rig.tick(181)
    assert "not finished" in rig.auto.status()["alarm"]


def test_arm_already_open_skips_startup(rig):
    rig.set_scope(parked=False)
    rig.tick()
    rig.tick(121)
    assert rig.scope.calls == ["import_schedule", ("start_scheduler", 1)]


def test_not_dark_never_starts(rig):
    rig.sun = -5.0
    rig.tick()
    rig.tick(500)
    assert rig.scope.calls == []
    assert rig.auto.status()["note"] == "waiting: not dark"


def test_missing_site_never_counts_as_dark(tmp_path):
    rig = Rig(tmp_path)
    rig.settings = replace(rig.settings, lat=0.0, lon=0.0)
    rig.tick()
    rig.tick(500)
    assert rig.scope.calls == []


def test_disabled_does_nothing(rig):
    rig.settings = replace(rig.settings, enabled=False)
    rig.tick()
    rig.tick(500)
    rig.safety = UNSAFE
    rig.set_scope(parked=False)
    rig.tick(100)
    rig.tick(100)
    assert rig.scope.calls == []


def test_unsafe_stops_in_the_verified_order(rig):
    rig.settle_and_start()
    rig.scope.calls.clear()
    rig.safety = UNSAFE
    rig.tick()
    rig.tick(20)
    assert rig.stop_calls() == []  # within the grace period
    rig.tick(11)
    assert rig.stop_calls() == ["clear_view_plan", "stop_scheduler", "stop_view", "park"]
    assert rig.auto.phase == PARKING


def test_brief_unsafe_blip_does_not_stop(rig):
    rig.settle_and_start()
    rig.scope.calls.clear()
    rig.safety = UNSAFE
    rig.tick()
    rig.safety = SAFE
    rig.tick(20)
    rig.tick(20)
    assert rig.stop_calls() == []


def test_failed_steps_never_skip_the_park(rig):
    rig.settle_and_start()
    rig.scope.fail = {"clear_view_plan": "536", "stop_view": "boom"}
    rig.safety = UNSAFE
    rig.tick()
    rig.tick(31)
    assert "park" in rig.scope.calls


def test_park_waits_for_threads_but_not_forever(rig):
    rig.settle_and_start()
    rig.scope.busy_flag = True
    rig.safety = UNSAFE
    rig.tick()
    rig.tick(31)
    assert "park" in rig.scope.calls


def test_park_is_verified_retried_then_alarmed(rig):
    rig.settle_and_start()
    rig.safety = UNSAFE
    rig.tick()
    rig.tick(31)
    rig.set_scope(sched_state="stopped")
    rig.tick(61)
    rig.tick(61)
    assert rig.scope.calls.count("park") == 3
    rig.tick(61)
    assert "PARK NOT CONFIRMED" in rig.auto.status()["alarm"]
    # and it keeps trying while the roof stays unsafe
    rig.tick(61)
    assert rig.scope.calls.count("park") == 4


def test_park_confirmed_clears_to_idle(rig):
    rig.settle_and_start()
    rig.safety = UNSAFE
    rig.tick()
    rig.tick(31)
    rig.set_scope(sched_state="stopped", parked=True)
    rig.tick(10)
    assert rig.auto.phase == IDLE


def _interrupt_at_item(rig, item):
    rig.set_scope(item_number=item)
    rig.tick(10)
    rig.safety = UNSAFE
    rig.tick()
    rig.tick(31)
    rig.set_scope(sched_state="stopped", parked=True, item_number=0)
    rig.tick(10)
    rig.scope.calls.clear()


def test_reopen_same_night_resumes_interrupted_item_and_reapplies_settings(rig):
    rig.settle_and_start()
    _interrupt_at_item(rig, 3)
    rig.safety = SAFE
    rig.tick(10)
    rig.tick(121)
    assert rig.scope.calls[0][0] == "open_arm"  # never start_polar_align
    rig.set_scope(parked=False, sched_state="complete")
    rig.tick(30)
    assert rig.scope.calls[1:] == [
        "import_schedule",
        ("apply_setting", "set_setting"),
        ("start_scheduler", 3),
    ]


def test_new_night_restarts_at_item_one(rig):
    rig.settle_and_start()
    _interrupt_at_item(rig, 3)
    rig.safety = SAFE
    rig.tick(24 * 3600)
    rig.tick(121)
    rig.set_scope(parked=False, sched_state="complete")
    rig.tick(30)
    assert ("start_scheduler", 1) in rig.scope.calls


def test_changing_the_schedule_drops_the_resume_point(rig):
    rig.settle_and_start()
    _interrupt_at_item(rig, 3)
    rig.write("Other.json", [_target("X"), _target("Y"), _target("Z")])
    rig.settings = replace(rig.settings, schedule="Other.json")
    rig.auto.request("schedule_changed")
    rig.safety = SAFE
    rig.tick(10)
    rig.tick(121)
    rig.set_scope(parked=False, sched_state="complete")
    rig.tick(30)
    assert ("start_scheduler", 1) in rig.scope.calls


def test_dawn_parks(rig):
    rig.settle_and_start()
    rig.scope.calls.clear()
    rig.sun = -14.0
    rig.tick(10)
    assert rig.stop_calls()[-1] == "park"


def test_daytime_does_not_park_a_scope_it_is_not_running(rig):
    rig.sun = 20.0
    rig.set_scope(parked=False)
    rig.tick()
    rig.tick(300)
    assert rig.scope.calls == []


def test_finished_schedule_parks_and_stays_parked_tonight(rig):
    rig.settle_and_start()
    rig.scope.calls.clear()
    rig.set_scope(sched_state="complete", item_number=0)
    rig.tick(10)
    assert rig.stop_calls()[-1] == "park"
    rig.set_scope(parked=True)
    rig.tick(10)
    rig.scope.calls.clear()
    rig.safety = UNSAFE
    rig.tick(10)
    rig.safety = SAFE
    rig.tick(10)
    rig.tick(500)
    assert rig.scope.calls == []
    assert rig.auto.status()["note"] == "schedule finished tonight"
    rig.tick(24 * 3600)
    rig.tick(121)
    assert rig.scope.calls[0][0] == "open_arm"


def test_foreign_app_plan_is_left_alone_until_unsafe(rig):
    rig.set_scope(parked=False, plan_state="working", view_state="working", view_stage="Stack")
    rig.tick()
    rig.tick(500)
    assert rig.scope.calls == []
    assert rig.auto.status()["foreign"] is True
    rig.safety = UNSAFE
    rig.tick()
    rig.tick(31)
    assert rig.stop_calls() == ["clear_view_plan", "stop_scheduler", "stop_view", "park"]


def test_idle_star_preview_is_not_a_foreign_session(rig):
    rig.set_scope(parked=False, view_state="working", view_stage="ContinuousExposure")
    rig.tick()
    rig.tick(121)
    assert ("start_scheduler", 1) in rig.scope.calls


def test_unknown_view_stage_counts_as_foreign(rig):
    rig.set_scope(parked=False, view_state="working", view_stage="SomethingNew")
    rig.tick()
    rig.tick(500)
    assert rig.scope.calls == []


def test_stop_button_holds_until_start(rig):
    rig.settle_and_start()
    rig.scope.calls.clear()
    rig.auto.request("stop")
    rig.tick(10)
    assert rig.stop_calls()[-1] == "park"
    rig.set_scope(sched_state="stopped", parked=True, item_number=0)
    rig.tick(10)
    rig.scope.calls.clear()
    rig.tick(500)
    assert rig.scope.calls == []
    assert rig.auto.status()["held"] is True
    rig.auto.request("start")
    rig.tick(10)
    assert rig.scope.calls[0][0] == "open_arm"


def test_hold_expires_next_night(rig):
    rig.auto.request("stop")
    rig.tick()
    rig.tick(500)
    rig.tick(10)
    rig.scope.calls.clear()
    rig.tick(24 * 3600)
    rig.tick(121)
    assert rig.scope.calls[0][0] == "open_arm"


def test_hold_survives_an_alp_restart(rig):
    rig.auto.request("stop")
    rig.tick()
    rig.tick(10)
    rig.scope.calls.clear()
    rig.auto = rig.build()
    rig.tick()
    rig.tick(500)
    assert rig.scope.calls == []


def test_manual_start_ignores_the_roof_and_is_not_parked_for_it(rig):
    rig.safety = UNSAFE
    rig.tick()
    rig.auto.request("start")
    rig.tick(1)
    assert rig.scope.calls[0][0] == "open_arm"
    assert rig.auto.status()["override_roof"] is True
    rig.set_scope(parked=False, sched_state="complete")
    rig.tick(30)
    rig.set_scope(sched_state="working", item_number=1)
    rig.tick(100)
    rig.tick(100)
    assert rig.stop_calls() == []


def test_manual_override_ends_once_roof_reads_safe_then_unsafe_parks(rig):
    rig.safety = UNSAFE
    rig.tick()
    rig.auto.request("start")
    rig.tick(1)
    rig.set_scope(parked=False, sched_state="complete")
    rig.tick(30)
    rig.set_scope(sched_state="working", item_number=1)
    rig.safety = SAFE
    rig.tick(100)
    assert rig.auto.status()["override_roof"] is False
    rig.safety = UNSAFE
    rig.tick(10)
    rig.tick(31)
    assert "park" in rig.scope.calls


def test_manual_start_still_requires_dark(rig):
    rig.sun = -5.0
    rig.auto.request("start")
    rig.tick()
    assert rig.scope.calls == []
    assert any("not dark" in line for line in rig.auto.status()["log"])


def test_manual_start_refuses_over_a_foreign_session(rig):
    rig.set_scope(parked=False, plan_state="working")
    rig.auto.request("start")
    rig.tick()
    assert rig.scope.calls == []


def test_second_stop_press_repeats_the_park_and_resets_its_attempts(rig):
    """Not harmless, which is why Tonight disables its button once pressed."""
    rig.settle_and_start()
    rig.auto.request("stop")
    rig.tick()
    assert rig.auto.phase == PARKING
    first = rig.scope.calls.count("park")
    rig.tick(61)  # no confirmation yet, so a second park attempt
    assert rig.auto._park_attempts == 2
    rig.auto.request("stop")
    rig.tick()
    assert rig.scope.calls.count("stop_view") == 2
    assert rig.scope.calls.count("park") == first + 2
    assert rig.auto._park_attempts == 1, "the count toward PARK NOT CONFIRMED restarts"


def test_scheduler_stopped_elsewhere_holds_instead_of_restarting(rig):
    rig.settle_and_start()
    rig.scope.calls.clear()
    rig.set_scope(sched_state="stopped", item_number=0)
    rig.tick(20)
    rig.tick(500)
    assert rig.scope.calls == []
    assert rig.auto.status()["held"] is True


def test_start_failures_stop_retrying_after_the_limit(rig):
    rig.scope.fail = {"open_arm": "Device is busy"}
    rig.tick()
    for _ in range(8):
        rig.tick(130)
    assert rig.scope.calls.count(("open_arm", 31.5, -99.37)) == 3


def test_rejected_schedule_never_commands_the_scope(rig):
    rig.write("Good.json", [{"action": "start_up_sequence", "params": {"3ppa": True}}, _target("A")])
    rig.tick()
    rig.tick(121)
    assert rig.scope.calls == []
    assert "3ppa" in rig.auto.status()["alarm"]


def test_disconnected_scope_alarms_when_it_should_park(rig):
    rig.settle_and_start()
    rig.scope.calls.clear()
    rig.safety = UNSAFE
    rig.set_scope(connected=False, parked=None, sched_state="unknown")
    rig.tick()
    rig.tick(31)
    assert rig.scope.calls == []
    assert "not connected" in rig.auto.status()["alarm"]


def test_safety_read_exception_style_reading_is_unsafe_and_parks(rig):
    rig.settle_and_start()
    rig.safety = SafetyReading(False, None, None, "read failed")
    rig.tick()
    rig.tick(31)
    assert "park" in rig.scope.calls


def test_dry_run_sends_nothing_to_the_real_device_and_walks_transitions(tmp_path):
    class Dev:
        is_connected = True
        device_name = "x"
        schedule = {"state": "stopped", "list": [], "item_number": 0}
        scheduler_thread = None
        framing_status = None
        sent = []

        def send_message_param_sync(self, data):
            Dev.sent.append(data["method"])
            if data["method"] == "get_device_state":
                return {"result": {"mount": {"close": True, "tracking": False}}}
            return {"result": {"View": {"state": "cancel"}}}

        def __getattr__(self, name):
            raise AssertionError(f"dry run touched device.{name}")

    rig = Rig(tmp_path)
    rig.settings = replace(rig.settings, dry_run=True)
    dry = DryRunScope(RealScope(lambda: Dev()))
    rig.scope = dry
    rig.tick()
    rig.tick(121)
    assert rig.auto.phase == OPENING
    rig.tick(10)
    assert rig.auto.phase == IMAGING
    rig.tick(20)
    rig.safety = UNSAFE
    rig.tick()
    rig.tick(31)
    rig.tick(10)
    assert rig.auto.phase == IDLE
    assert set(Dev.sent) <= {"get_device_state", "iscope_get_app_state"}
    assert all("[DRY RUN]" in line for line in rig.auto.status()["log"])


def test_only_literal_false_arms(monkeypatch):
    from device.config import Config
    from roof import config as roof_config

    for value, expected in [(False, False), (True, True), ("false", True), (0, True)]:
        monkeypatch.setattr(
            Config,
            "get_toml",
            lambda sect, item, default, v=value: v if item == "dry_run" else default,
        )
        assert roof_config.load().dry_run is expected


# ---- real device shape ------------------------------------------------------


class ResultDev:
    is_connected = True
    device_name = "x"
    scheduler_thread = None

    def __init__(self):
        self.schedule = {"state": "working", "list": [_target("A")], "item_number": 9999}
        self.calls = []

    def send_message_param_sync(self, data):
        self.calls.append(dict(data))
        return {"result": "Error: Exceeded allotted wait time for result", **data}

    def action_start_up_sequence(self, params):
        self.calls.append(params)
        return {"command": "start_up_sequence", "code": 0, "result": "Sequence started."}

    def import_schedule(self, params):
        return {"code": -1, "result": "An existing scheduler is active."}

    def start_scheduler(self, params):
        return self.schedule


def test_real_scope_treats_sync_timeout_as_failure_and_unknown_mount():
    dev = ResultDev()
    scope = RealScope(lambda: dev)
    assert "Exceeded" in scope.park()
    state = scope.read()
    assert state.parked is None
    assert state.item_number == 0  # 9999 is ALP's "no item" marker


def test_real_scope_open_arm_never_aligns():
    dev = ResultDev()
    assert RealScope(lambda: dev).open_arm(31.5, -99.37) is None
    assert dev.calls[0] == {
        "lat": 31.5,
        "lon": -99.37,
        "3ppa": False,
        "auto_focus": False,
        "dark_frames": False,
    }


def test_real_scope_reports_device_refusals_and_accepts_schedule_reply():
    scope = RealScope(lambda: ResultDev())
    assert "existing scheduler" in scope.import_schedule("x")
    assert scope.start_scheduler(1) is None


# ---- sentinel ---------------------------------------------------------------


class _Sentinel(BaseHTTPRequestHandler):
    replies = {}
    puts = []

    def do_GET(self):
        member = self.path.split("?")[0].rsplit("/", 1)[-1]
        reply = self.replies.get(member)
        if reply is None:
            self.send_error(500)
            return
        body = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", 0))
        self.puts.append(self.rfile.read(length).decode())
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


@pytest.fixture
def sentinel_url():
    server = HTTPServer(("127.0.0.1", 0), _Sentinel)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Sentinel.puts = []
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _value(v):
    return {"ErrorNumber": 0, "Value": v}


def test_sentinel_safe_only_when_connected_and_safe(sentinel_url):
    _Sentinel.replies = {"connected": _value(True), "issafe": _value(True)}
    assert sentinel.read(sentinel_url, 0).safe is True


@pytest.mark.parametrize(
    "replies",
    [
        {"connected": _value(True), "issafe": _value(False)},
        {"connected": _value(True), "issafe": _value("true")},
        {"connected": _value(True), "issafe": _value(None)},
        {"connected": _value(True), "issafe": {"ErrorNumber": 1031, "ErrorMessage": "x"}},
        {"connected": _value(True)},
        {"connected": _value(None), "issafe": _value(True)},
        {},
    ],
)
def test_sentinel_everything_else_is_unsafe(sentinel_url, replies):
    _Sentinel.replies = replies
    assert sentinel.read(sentinel_url, 0).safe is False


def test_sentinel_disconnected_reconnects_but_reads_unsafe(sentinel_url):
    _Sentinel.replies = {"connected": _value(False), "issafe": _value(True)}
    reading = sentinel.read(sentinel_url, 0)
    assert reading.safe is False
    assert "Connected=True" in _Sentinel.puts[0]


def test_sentinel_unreachable_is_unsafe():
    assert sentinel.read("http://127.0.0.1:9", 0).safe is False


# ---- sun --------------------------------------------------------------------


@pytest.mark.parametrize("hour", [0, 3, 6, 9, 12, 15, 18, 21])
def test_sun_matches_astropy(hour):
    astropy_time = pytest.importorskip("astropy.time")
    coords = pytest.importorskip("astropy.coordinates")
    units = pytest.importorskip("astropy.units")
    when = datetime(2026, 11, 5, hour, 0, tzinfo=timezone.utc)
    site = coords.EarthLocation(lat=31.5010 * units.deg, lon=-99.3734 * units.deg)
    t = astropy_time.Time(when)
    expected = coords.get_sun(t).transform_to(coords.AltAz(obstime=t, location=site)).alt.deg
    assert sun_altitude_deg(31.5010, -99.3734, when) == pytest.approx(expected, abs=1.0)


def test_night_key_spans_midnight_and_changes_at_solar_noon():
    lon = -99.37
    evening = datetime(2026, 11, 5, 2, 0, tzinfo=timezone.utc).timestamp()
    before_dawn = datetime(2026, 11, 5, 11, 0, tzinfo=timezone.utc).timestamp()
    next_evening = datetime(2026, 11, 6, 2, 0, tzinfo=timezone.utc).timestamp()
    assert night_key(evening, lon) == night_key(before_dawn, lon)
    assert night_key(next_evening, lon) != night_key(evening, lon)


# ---- schedules --------------------------------------------------------------


@pytest.mark.parametrize(
    "item",
    [
        {"action": "start_polar_align"},
        {"action": "shutdown"},
        {"action": "start_up_sequence", "params": {"3ppa": True}},
        {"action": "start_mosaic", "params": {"session_time_sec": 60}},
    ],
)
def test_validator_rejects(item):
    errors, _ = schedule_tools.validate_items([item])
    assert errors


def test_validator_warns_about_exposure_item_without_a_handler():
    errors, warnings = schedule_tools.validate_items(
        [{"action": "set_setting_exposures", "params": {}}, _target("A")]
    )
    assert not errors and "set_setting" in warnings[0]


def test_resolve_only_returns_listed_files(tmp_path):
    (tmp_path / "ok.json").write_text("{}")
    assert schedule_tools.resolve("ok.json", str(tmp_path))
    assert schedule_tools.resolve("../ok.json", str(tmp_path)) is None
    assert schedule_tools.resolve("/etc/passwd", str(tmp_path)) is None


def test_plan_conversion_single_and_framed():
    schedule = schedule_tools.plan_to_schedule(
        {
            "list": [
                {"target_name": "S", "target_ra_dec": [1.5, 2.5], "lp_filter": True},
                {
                    "target_name": "M",
                    "target_ra_dec": [3, 4],
                    "mosaic": {"scale": 1.7, "angle": 170},
                },
            ]
        }
    )
    first, single, framed = schedule["list"]
    assert first["action"] == "set_setting"
    assert first["params"]["exp_ms"]["stack_l"] == 30000
    assert single["action"] == "start_mosaic" and single["params"]["ra_num"] == 1
    assert single["params"]["is_use_lp_filter"] is True
    assert framed["action"] == "start_framed_mosaic"
    assert framed["params"]["mosaic_angle"] == -10
    assert not schedule_tools.validate_items(schedule["list"])[0]


def test_shipped_schedules_are_valid():
    errors = {}
    for name in schedule_tools.list_schedules():
        errors[name] = schedule_tools.load(schedule_tools.resolve(name))[1]
    assert not any(errors.values()), errors


# ---- web ----------------------------------------------------------------------


class _FakeAutomation:
    def __init__(self):
        self.requests = []

    def request(self, command):
        self.requests.append(command)

    def start(self):
        pass

    def status(self):
        return {"safe": True, "log": [], "loop_stale": False, "dry_run": True}


@pytest.fixture
def web(monkeypatch):
    import falcon
    import falcon.testing

    from roof import web as roof_web

    fake = _FakeAutomation()
    monkeypatch.setattr(roof_web, "get_automation", lambda: fake)
    app = falcon.App()
    roof_web.install(app)
    return falcon.testing.TestClient(app), fake


FORM = {"Content-Type": "application/x-www-form-urlencoded"}


@pytest.mark.parametrize("headers", [None, FORM])
@pytest.mark.parametrize("path,command", [("/roof/stop", "stop"), ("/1/roof/start", "start")])
def test_buttons_work_with_or_without_a_form_body(web, path, command, headers):
    client, fake = web
    result = client.simulate_post(path, headers=headers)
    assert result.status_code == 302
    assert fake.requests == [command]


def test_cross_origin_post_is_refused(web):
    client, fake = web
    result = client.simulate_post(
        "/roof/stop", headers={"Origin": "http://evil.example", "Host": "localhost:5432"}
    )
    assert result.status_code == 403
    assert fake.requests == []


def test_same_origin_post_is_allowed(web):
    client, fake = web
    result = client.simulate_post(
        "/roof/stop", headers={"Origin": "http://localhost:5432", "Host": "localhost:5432"}
    )
    assert result.status_code == 302


def test_select_refuses_unlisted_paths(web):
    client, fake = web
    result = client.simulate_post(
        "/roof/select", headers=FORM, body="schedule=../../etc/passwd"
    )
    assert result.status_code == 400
    assert fake.requests == []


def test_form_save_keeps_the_roof_section(tmp_path, monkeypatch):
    from device.config import Config

    path = tmp_path / "config.toml"
    monkeypatch.setitem(Config._dict, "roof", {"schedule": "X.json", "dry_run": True})
    Config.set_toml("seestar_initialization", "lat", 31.5)
    Config.save_toml(str(path))
    text = path.read_text()
    assert "[roof]" in text and 'schedule = "X.json"' in text


# ---- darkness and altitude gates ---------------------------------------------


@pytest.fixture
def alt_rig(tmp_path):
    rig = Rig(tmp_path)
    rig.settings = replace(rig.settings, min_target_alt_deg=30.0)
    return rig


def test_roof_open_at_5pm_waits_parked_until_astronomical_dark(rig):
    rig.settings = replace(rig.settings, sun_max_deg=-18.0)
    rig.sun = 10.0
    rig.tick()
    rig.tick(3600)
    rig.sun = -17.9
    rig.tick(3600)
    assert rig.scope.calls == []
    rig.sun = -18.1
    rig.tick(10)
    assert rig.scope.calls[0][0] == "open_arm"


def test_parks_at_astronomical_dawn_with_roof_still_open(rig):
    rig.settings = replace(rig.settings, sun_max_deg=-18.0)
    rig.settle_and_start()
    rig.scope.calls.clear()
    rig.sun = -17.9
    rig.tick(10)
    assert rig.safety.safe is True
    assert rig.stop_calls()[-1] == "park"
    # and it doesn't start again in the morning
    rig.set_scope(parked=True, sched_state="stopped", item_number=0)
    rig.tick(10)
    rig.scope.calls.clear()
    rig.tick(600)
    assert rig.scope.calls == []


def test_waits_until_a_target_is_high_enough(alt_rig):
    alt_rig.alts = {"A": 10.0, "B": 29.9}
    alt_rig.tick()
    alt_rig.tick(500)
    assert alt_rig.scope.calls == []
    assert "above 30" in alt_rig.auto.status()["note"]
    alt_rig.alts = {"A": 10.0, "B": 30.5}
    alt_rig.tick(10)
    assert alt_rig.scope.calls[0][0] == "open_arm"


def test_starts_at_first_high_enough_target_and_keeps_settings(alt_rig):
    alt_rig.alts = {"A": 10.0, "B": 50.0}
    alt_rig.tick()
    alt_rig.tick(121)
    alt_rig.set_scope(sched_state="complete")
    alt_rig.tick(10)
    assert alt_rig.scope.calls[1:] == [
        "import_schedule",
        ("apply_setting", "set_setting"),
        ("start_scheduler", 3),
    ]


def test_current_target_falling_below_limit_is_skipped_once(alt_rig):
    alt_rig.settle_and_start()
    alt_rig.set_scope(item_number=2)
    alt_rig.tick(10)
    assert "skip_item" not in alt_rig.scope.calls
    alt_rig.alts = {"A": 29.0}
    alt_rig.tick(10)
    alt_rig.tick(10)
    assert alt_rig.scope.calls.count("skip_item") == 1
    alt_rig.set_scope(item_number=3)
    alt_rig.alts = {"A": 29.0, "B": 29.0}
    alt_rig.tick(10)
    assert alt_rig.scope.calls.count("skip_item") == 2


def test_resume_moves_past_a_target_now_too_low(alt_rig):
    alt_rig.settle_and_start()
    alt_rig.set_scope(item_number=2)
    alt_rig.tick(10)
    alt_rig.safety = UNSAFE
    alt_rig.tick()
    alt_rig.tick(31)
    alt_rig.set_scope(sched_state="stopped", parked=True, item_number=0)
    alt_rig.tick(10)
    alt_rig.scope.calls.clear()
    alt_rig.alts = {"A": 10.0}
    alt_rig.safety = SAFE
    alt_rig.tick(10)
    alt_rig.tick(121)
    alt_rig.set_scope(sched_state="complete")
    alt_rig.tick(10)
    assert ("start_scheduler", 3) in alt_rig.scope.calls


def test_manual_start_refused_when_nothing_is_high_enough_and_keeps_hold(alt_rig):
    alt_rig.alts = {"A": 5.0, "B": 5.0}
    alt_rig.auto.request("stop")
    alt_rig.tick()
    alt_rig.auto.request("start")
    alt_rig.scope.calls.clear()
    alt_rig.tick(10)
    assert alt_rig.scope.calls == []
    assert alt_rig.auto.status()["held"] is True


def test_altitude_failure_counts_as_too_low(alt_rig):
    def boom(*args):
        raise ValueError("bad coordinates")

    alt_rig.auto._target_altitude = boom
    alt_rig.tick()
    alt_rig.tick(500)
    assert alt_rig.scope.calls == []


def test_gate_off_when_zero(rig):
    rig.alts = {"A": -50.0, "B": -50.0}
    rig.tick()
    rig.tick(121)
    assert rig.scope.calls[0][0] == "open_arm"


def test_target_altitude_matches_astropy_altaz():
    coords = pytest.importorskip("astropy.coordinates")
    time_mod = pytest.importorskip("astropy.time")
    units = pytest.importorskip("astropy.units")
    from roof.targets import target_altitude_deg

    when = datetime(2026, 11, 5, 8, 0, tzinfo=timezone.utc)
    site = coords.EarthLocation(lat=31.5 * units.deg, lon=-99.37 * units.deg)
    t = time_mod.Time(when)
    m42 = coords.SkyCoord(ra=5.588 * units.hourangle, dec=-5.39 * units.deg, frame="icrs")
    expected = m42.transform_to(coords.AltAz(obstime=t, location=site)).alt.deg
    params = {"ra": 5.588, "dec": -5.39, "is_j2000": True}
    assert target_altitude_deg(params, 31.5, -99.37, when) == pytest.approx(expected, abs=0.2)
    assert target_altitude_deg({"ra": -1, "dec": -1}, 31.5, -99.37, when) is None


def test_headline_says_what_it_is_doing(rig):
    rig.sun = -5.0
    rig.tick()
    rig.tick(121)
    assert rig.auto.status()["headline"] == "Waiting: not dark"
    rig.sun = -40.0
    rig.now += 3600  # long enough that the restart can't carry the roof's safe time
    rig.auto = rig.build()
    rig.settle_and_start()
    # Item 1 of the schedule is a setting, so item 2 of 3 is target 1 of 2.
    rig.set_scope(target="M 42", item_number=2, item_count=3)
    rig.tick(10)
    assert rig.auto.status()["headline"] == "Imaging M 42 (target 1 of 2)"


def test_disconnected_scope_is_the_reason_shown_first(rig):
    rig.safety = UNSAFE
    rig.set_scope(connected=False, parked=None, sched_state="unknown")
    rig.tick()
    assert rig.auto.status()["note"] == "waiting: scope not connected"


@pytest.mark.parametrize(
    "path,query,location",
    [
        ("/roof", None, "/1/roof"),
        ("/roof/status", "a=b", "/1/roof/status?a=b"),
    ],
)
def test_pages_without_a_device_number_go_to_the_scope(web, path, query, location):
    client, _ = web
    result = client.simulate_get(path, query_string=query)
    assert result.status_code == 302
    assert result.headers["location"].endswith(location)


def test_explicit_federation_and_unnumbered_only_routes_are_untouched(web):
    client, _ = web
    assert "location" not in client.simulate_get("/0/roof").headers
    assert "location" not in client.simulate_get("/roof/badge").headers


def _framed(name, scale=1.7):
    return {
        "action": "start_framed_mosaic",
        "params": {"target_name": name, "panel_time_sec": 60, "mosaic_scale": scale},
    }


def test_framing_mismatch_alarms_and_match_is_logged(rig):
    rig.write("Good.json", [_framed("A"), _framed("B")])
    rig.settle_and_start()
    rig.set_scope(view_stage="Stack", mosaic_scale=1.0, item_number=1)
    rig.tick(10)
    assert "not the planned 1.7" in rig.auto.status()["alarm"]
    rig.auto._alarm = ""
    rig.set_scope(item_number=2, mosaic_scale=1.7)
    rig.tick(10)
    assert rig.auto.status()["alarm"] == ""
    assert any("framing confirmed" in line for line in rig.auto.status()["log"])


def test_plan_conversion_keeps_star_map_angle():
    schedule = schedule_tools.plan_to_schedule(
        {"list": [{"target_name": "M", "target_ra_dec": [3, 4],
                   "mosaic": {"scale": 1.7, "angle": 67.0, "star_map_angle": 69.05}}]}
    )
    assert schedule["list"][1]["params"]["mosaic_star_map_angle"] == 69.05


@pytest.mark.parametrize(
    "item,label",
    [
        (_target("A"), "Single frame"),
        (
            {"action": "start_mosaic", "params": {"ra_num": 2, "dec_num": 3}},
            "Mosaic 2x3 panels",
        ),
        (
            {
                "action": "start_framed_mosaic",
                "params": {"mosaic_scale": 1.7, "mosaic_angle": 67.0},
            },
            "Framed mosaic 1.7x at 67°",
        ),
    ],
)
def test_describe_target(item, label):
    assert schedule_tools.describe_target(item) == label


# ---- dew heater -----------------------------------------------------------------


class _FixedDew:
    def __init__(self, reading):
        self.reading = reading

    def read(self, s):
        return self.reading


class _DewScope(FakeScope):
    def set_heater(self, power):
        return self._record("set_heater", power)


@pytest.fixture
def dew_rig(tmp_path):
    from roof.dew import DewReading

    rig = Rig(tmp_path)
    rig.scope = _DewScope()
    rig.settings = replace(rig.settings, dew_enabled=True)
    rig.dew = _FixedDew(DewReading(12.0, 2.0, "test"))
    rig.auto = RoofAutomation(
        load_settings=lambda: rig.settings,
        read_safety=lambda s: rig.safety,
        sun_altitude=lambda lat, lon, when: rig.sun,
        scope_factory=lambda s: rig.scope,
        clock=lambda: rig.now,
        schedule_dir=str(rig.dir),
        sleep_wait=lambda s: None,
        target_altitude=lambda *a: 60.0,
        dew_monitor=rig.dew,
    )
    return rig


def _heater_calls(rig):
    return [c[1] for c in rig.scope.calls if isinstance(c, tuple) and c[0] == "set_heater"]


def test_dew_off_when_margin_is_wide_and_start_state_is_confirmed(dew_rig):
    dew_rig.settle_and_start()
    # Once while parked (state unknown after a restart), once after start-up.
    assert _heater_calls(dew_rig) == [0, 0]


def test_dew_turns_on_with_hysteresis(dew_rig):
    from roof.dew import DewReading

    dew_rig.settle_and_start()
    dew_rig.dew.reading = DewReading(12.0, 8.0, "test")  # margin 4
    dew_rig.tick(10)
    assert _heater_calls(dew_rig) == [0, 0, 30]
    dew_rig.dew.reading = DewReading(12.0, 7.0, "test")  # margin 5: holds
    dew_rig.tick(10)
    assert _heater_calls(dew_rig) == [0, 0, 30]
    dew_rig.dew.reading = DewReading(12.0, 6.0, "test")  # margin 6: off
    dew_rig.tick(10)
    assert _heater_calls(dew_rig) == [0, 0, 30, 0]


def test_dew_stays_on_with_no_weather_data(dew_rig):
    dew_rig.dew.reading = None
    dew_rig.settle_and_start()
    assert _heater_calls(dew_rig) == [0, 30]


def test_dew_off_when_session_parks(dew_rig):
    from roof.dew import DewReading

    dew_rig.dew.reading = DewReading(12.0, 11.0, "test")
    dew_rig.settle_and_start()
    assert _heater_calls(dew_rig) == [0, 30]
    dew_rig.safety = UNSAFE
    dew_rig.tick()
    dew_rig.tick(31)
    dew_rig.set_scope(sched_state="stopped", parked=True, item_number=0)
    dew_rig.tick(10)
    dew_rig.tick(10)
    assert _heater_calls(dew_rig) == [0, 30, 0]


def test_dew_leaves_a_foreign_session_alone(dew_rig):
    dew_rig.set_scope(parked=False, plan_state="working")
    dew_rig.tick()
    dew_rig.tick(500)
    assert _heater_calls(dew_rig) == []


def test_dew_failure_retries_next_tick(dew_rig):
    from roof.dew import DewReading

    dew_rig.dew.reading = DewReading(12.0, 11.0, "test")
    dew_rig.scope.fail = {"set_heater": "boom"}
    dew_rig.settle_and_start()
    assert len(_heater_calls(dew_rig)) >= 2


def test_dew_point_and_smallest_margin_wins():
    from roof.dew import DewMonitor, DewReading, dew_point_c

    assert dew_point_c(12.6, 69) == pytest.approx(7.06, abs=0.1)
    t = [0.0]
    calls = []
    monitor = DewMonitor(
        clock=lambda: t[0] + 1000,
        readers=[
            lambda: calls.append(1) or DewReading(13.7, 8.2, "a"),
            lambda: (_ for _ in ()).throw(ValueError()),
            lambda: DewReading(11.7, 8.9, "b"),
        ],
    )
    assert monitor.read(None).source == "b"
    assert monitor.read(None).source == "b"
    assert len(calls) == 1  # cached


def test_dew_turned_off_after_a_restart_while_parked_only(dew_rig):
    dew_rig.tick()
    assert _heater_calls(dew_rig) == [0]
    dew_rig.scope.calls.clear()
    dew_rig.auto._heater_on = None
    dew_rig.set_scope(parked=False)
    dew_rig.tick(10)
    assert _heater_calls(dew_rig) == []  # arm open, maybe someone else's session


def _guard(seq, state, note="", item=1):
    return {"seq": seq, "state": state, "item_number": item, "target_name": "A", "note": note}


def test_guard_retrying_then_restored_stays_visible_and_changes_no_decision(rig):
    rig.write("Good.json", [_framed("A"), _framed("B")])
    rig.settle_and_start()
    calls = list(rig.scope.calls)
    rig.set_scope(
        view_stage="Stack",
        mosaic_scale=1.0,
        item_number=1,
        framing=_guard(1, "retrying", "A is stacking at scale 1, not the planned 1.7; retrying once"),
    )
    rig.tick(10)
    assert rig.auto.status()["alarm"].endswith("retrying once")
    # Not overwritten by the backstop, which waits for the retry's outcome.
    rig.tick(10)
    assert rig.auto.status()["alarm"].endswith("retrying once")
    rig.set_scope(
        mosaic_scale=1.7,
        framing=_guard(2, "restored", "A lost its framing, and one retry restored scale 1.7"),
    )
    rig.tick(10)
    assert "one retry restored" in rig.auto.status()["alarm"]
    assert any("framing confirmed" in line for line in rig.auto.status()["log"])
    assert rig.auto.phase == IMAGING
    assert rig.scope.calls == calls


def test_guard_skip_on_the_last_item_stays_on_tonight_after_the_park(rig):
    rig.write("Good.json", [_framed("A")])
    rig.settle_and_start()
    note = "A skipped: asked for scale 1.7 angle 67, and after one retry it would not hold"
    rig.set_scope(view_stage="", item_number=0, sched_state="complete", framing=_guard(1, "skipped", note))
    rig.tick(10)
    assert rig.auto.status()["alarm"] == note
    # The finish parks as it always does, with the same stop sequence.
    assert rig.stop_calls() == ["clear_view_plan", "stop_scheduler", "stop_view", "park"]
    rig.set_scope(parked=True)
    rig.tick(10)
    assert rig.auto.phase == IDLE
    assert rig.auto.status()["alarm"] == note
    assert sum(note in line for line in rig.auto.status()["log"]) == 1


def test_guard_skip_leaves_the_target_out_and_replans(rig):
    rig.settings = replace(rig.settings, schedule=schedule_tools.TARGET_LIST)
    rig.auto._active_schedule = schedule_tools.TARGET_LIST
    rig.auto._active_items = [_framed("A")]
    rig.auto._launched_at = rig.now
    rig.auto.phase = IMAGING
    rig.set_scope(connected=True, parked=False, sched_state="complete", framing=_guard(1, "skipped", "A skipped."))
    rig.tick(1)
    status = rig.auto.status()
    assert status["alarm"].startswith("A skipped.") and "ended well before" not in status["alarm"]
    assert status["completed"] is False
    assert rig.auto._failed_targets["A"]["why"] == "its framing would not hold"


def test_guard_skip_on_an_item_is_not_overwritten_by_the_backstop(rig):
    rig.write("Good.json", [_framed("A"), _framed("B")])
    rig.settle_and_start()
    rig.set_scope(view_stage="Stack", mosaic_scale=1.0, item_number=1, framing=_guard(1, "skipped", "A skipped."))
    rig.tick(10)
    rig.tick(10)
    assert rig.auto.status()["alarm"] == "A skipped."
    # Reported once, not again on later ticks.
    assert sum("ALARM: A skipped." in line for line in rig.auto.status()["log"]) == 1


def test_backstop_says_still_wrong_when_the_guard_passed_but_it_changed_later(rig):
    rig.write("Good.json", [_framed("A")])
    rig.settle_and_start()
    rig.set_scope(view_stage="Stack", mosaic_scale=1.0, item_number=1, framing=_guard(1, "held"))
    rig.tick(10)
    alarm = rig.auto.status()["alarm"]
    assert "not the planned 1.7" in alarm and "still wrong" in alarm


def test_backstop_message_is_unchanged_with_no_guard_outcome(rig):
    rig.write("Good.json", [_framed("A")])
    rig.settle_and_start()
    rig.set_scope(view_stage="Stack", mosaic_scale=1.0, item_number=1)
    rig.tick(10)
    assert rig.auto.status()["alarm"] == "A is stacking at scale 1, not the planned 1.7"


def test_guard_outcome_is_ignored_for_an_adopted_session(rig):
    rig.write("Good.json", [_framed("A")])
    rig.settle_and_start()
    rig.auto._adopted = True
    rig.set_scope(framing=_guard(1, "skipped", "A skipped."))
    rig.tick(10)
    assert rig.auto.status()["alarm"] == ""


def test_real_scope_passes_the_guard_outcome_and_dry_run_drops_it():
    outcome = _guard(1, "skipped", "A skipped.")

    class Dev:
        is_connected = True
        device_name = "x"
        schedule = {"state": "stopped", "list": [], "item_number": 0}
        scheduler_thread = None
        framing_status = outcome

        def send_message_param_sync(self, data):
            return {"result": {}}

    real = RealScope(lambda: Dev())
    assert real.read().framing == outcome
    assert DryRunScope(real).read().framing is None


def test_a_restart_with_the_roof_open_starts_without_waiting_out_the_settle_again(tmp_path):
    rig = Rig(tmp_path)
    rig.tick()
    rig.tick(121)
    assert rig.auto.phase == OPENING
    # Restarted from parked, a few seconds later, with the roof still open.
    rig.scope.state = replace(rig.scope.state, parked=True, sched_state="stopped")
    rig.tick(5)
    rig.auto = rig.build()
    rig.tick(30)
    assert rig.auto.phase == OPENING
    assert any("safe before the restart" in line for line in rig.auto.status()["log"])


def test_a_long_outage_still_waits_out_the_settle(tmp_path):
    from roof.automation import SAFE_CARRY_S

    rig = Rig(tmp_path)
    rig.tick()
    rig.tick(60)
    rig.auto = rig.build()
    rig.tick(SAFE_CARRY_S + 120)
    assert rig.auto.phase != OPENING
    assert "must read safe" in rig.auto.status()["note"]


def test_a_roof_that_closed_before_the_restart_waits_out_the_settle(tmp_path):
    rig = Rig(tmp_path)
    rig.tick()
    rig.tick(60)
    rig.safety = UNSAFE
    rig.tick(10)
    rig.safety = SAFE
    rig.auto = rig.build()
    rig.tick(10)
    assert rig.auto.phase != OPENING
