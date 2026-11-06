"""The framed-mosaic guard, against a fake firmware. No sockets, no real files."""

from pathlib import Path

import pytest

from device import framing_log
from device.seestar_device import FRAMING_RETRY_GOTO_TIMEOUT_S, Seestar


class Logger:
    def __init__(self):
        self.lines = []

    def _add(self, message, *args, **_kwargs):
        self.lines.append(str(message) % args if args else str(message))

    info = debug = warn = warning = error = _add


class Firmware:
    """Stores a mosaic given with a view start, as firmware 9.75 does.

    resets: how many iscope_start_stack calls put the mosaic back to scale 1.
    hook: called with each method as it's received, to play a stop or a failure.
    """

    def __init__(self, scope, monkeypatch, resets=0, goto_outcome="complete"):
        self.scope = scope
        self.resets = resets
        self.goto_outcome = goto_outcome
        self.mosaic = {"scale": 1.0, "angle": 0.0}
        self.sent = []
        self.sleeps = 0
        self.read_reply = None  # replaces get_device_state's reply when set
        self.hook = lambda method: None
        self.stack_running = False
        monkeypatch.setattr(scope, "send_message_param_sync", self.receive)
        monkeypatch.setattr("device.seestar_device.time.sleep", self.sleep)
        monkeypatch.setattr("device.seestar_device.sleep", self.sleep)

    def methods(self, name):
        return [m for m in self.sent if m["method"] == name]

    def order(self):
        return [m["method"] for m in self.sent]

    def sleep(self, _seconds):
        # Events reach the device while a thread sleeps.
        self.sleeps += 1
        event_state = self.scope.event_state
        if event_state.get("AutoGoto", {}).get("state") == "start" and self.goto_outcome:
            event_state["AutoGoto"]["state"] = self.goto_outcome
        if event_state.get("AutoFocus", {}).get("state") == "working":
            event_state["AutoFocus"]["state"] = "complete"

    def receive(self, data):
        method = data["method"]
        self.sent.append(data)
        self.hook(method)
        if method == "get_device_state":
            if self.read_reply is not None:
                reply = self.read_reply
                if isinstance(reply, Exception):
                    raise reply
                return reply
            return {"result": {"setting": {"mosaic": dict(self.mosaic)}}}
        if method == "iscope_start_view" and data["params"].get("mosaic"):
            self.mosaic = dict(data["params"]["mosaic"])
        if method == "iscope_start_stack":
            self.stack_running = True
            if self.resets > 0:
                self.resets -= 1
                self.mosaic = {"scale": 1.0, "angle": 0.0, "star_map_angle": 361.0}
        if method == "iscope_stop_view" and data["params"].get("stage") == "Stack":
            self.stack_running = False
        return {"result": 0, "code": 0}


@pytest.fixture
def scope():
    device = Seestar(Logger(), "127.0.0.1", 4700, "TestScope", 1, True)
    device.schedule["state"] = "working"
    device.schedule["is_skip_requested"] = False
    device.schedule["current_item_id"] = "item"
    device.schedule["item_number"] = 2
    device.event_state["scheduler"] = {"cur_scheduler_item": {}}
    return device


@pytest.fixture
def diag(tmp_path):
    framing_log.enable(str(tmp_path))
    yield tmp_path / framing_log.FILE_NAME
    framing_log.disable()


def run(scope, scale=1.7, angle=67.0, autofocus=True, panel_time=10, stack_type="DeepSky"):
    scope.framed_mosaic_thread_fn(
        "IC 1805", 2.74, 61.3, True, panel_time, scale, angle, 200, autofocus, 1, 5, stack_type
    )


def action(scope):
    return scope.event_state["scheduler"]["cur_scheduler_item"]["action"]


def view_starts(firmware):
    return firmware.methods("iscope_start_view")


def stops(firmware, stage):
    return [
        m
        for m in firmware.methods("iscope_stop_view")
        if m.get("params", {}).get("stage") == stage
    ]


def test_holds_at_first_stacks_with_no_retry_and_logs_each_step(scope, monkeypatch, diag):
    firmware = Firmware(scope, monkeypatch)
    run(scope)
    assert len(view_starts(firmware)) == 1
    assert len(firmware.methods("iscope_start_stack")) == 1
    assert action(scope) == "complete"
    assert scope.framing_status["state"] == "held"
    assert scope.is_cur_scheduler_item_working is False
    text = diag.read_text()
    for step in ("after_goto", "after_autofocus", "after_set_stack_type", "after_start_stack"):
        assert f"IC 1805 {step}: asked scale 1.7 angle 67.0 scope scale 1.7 angle 67.0" in text
    assert "MISMATCH" not in text
    assert len(firmware.methods("get_device_state")) == 4
    # The stack type still goes first, then the stack.
    order = firmware.order()
    assert order.index("set_stack_type") < order.index("iscope_start_stack")


def test_resets_once_then_holds_after_the_retry(scope, monkeypatch, diag):
    firmware = Firmware(scope, monkeypatch, resets=1)
    run(scope)
    starts = view_starts(firmware)
    assert len(starts) == 2
    assert starts[1]["params"]["mosaic"] == starts[0]["params"]["mosaic"]
    assert starts[1]["params"]["target_ra_dec"] == starts[0]["params"]["target_ra_dec"]
    # No second autofocus, and the second stack is set up like the first.
    assert len(firmware.methods("start_auto_focuse")) == 1
    assert len(firmware.methods("iscope_start_stack")) == 2
    assert len(firmware.methods("set_stack_type")) == 2
    assert len(firmware.methods("set_sequence_setting")) == 2
    lp = [m for m in firmware.methods("set_setting") if "stack_lenhance" in m["params"]]
    assert lp[-1]["params"]["stack_lenhance"] is True
    # Order: stop the wrong stack, view start, stack again.
    order = firmware.order()
    second_view = len(order) - 1 - order[::-1].index("iscope_start_view")
    assert "iscope_stop_view" in order[:second_view]
    assert "iscope_start_stack" in order[second_view:]
    assert scope.framing_status["state"] == "restored"
    assert "restored scale 1.7" in scope.framing_status["note"]
    assert action(scope) == "complete"
    text = diag.read_text()
    assert "after_start_stack: asked scale 1.7 angle 67.0 scope scale 1 angle 0" in text
    assert "MISMATCH" in text
    assert "retry_after_view" in text and "retry_after_start_stack" in text
    assert "guard: retrying" in text and "guard: restored" in text
    assert any("scope reports scale 1 angle 0" in line for line in scope.logger.lines)


def test_never_holds_stops_records_and_ends_the_item(scope, monkeypatch, diag):
    firmware = Firmware(scope, monkeypatch, resets=99)
    run(scope, panel_time=10920)
    assert len(firmware.methods("iscope_start_stack")) == 2
    assert firmware.stack_running is False
    assert scope.framing_status["state"] == "skipped"
    note = scope.framing_status["note"]
    assert "asked for scale 1.7 angle 67.0" in note and "scale 1 angle 0" in note
    assert "skipped" in action(scope)
    assert scope.is_cur_scheduler_item_working is False
    assert firmware.sleeps < 30  # it did not sit through the 3 hour stacking loop
    # The mosaic reset in the finally still runs.
    resets = [m for m in firmware.methods("set_setting") if "mosaic" in m["params"]]
    assert resets[-1]["params"]["mosaic"]["scale"] == 1.0
    assert "guard: skipped" in diag.read_text()


def test_never_holds_lets_the_schedule_move_to_its_next_item(scope, monkeypatch):
    firmware = Firmware(scope, monkeypatch, resets=99)
    framed = {
        "action": "start_framed_mosaic",
        "schedule_item_id": "framed",
        "params": {},
    }
    nextone = {"action": "next_item_marker", "schedule_item_id": "next", "params": {}}
    scope.schedule.update(list=[framed, nextone], item_number=1, state="stopped")
    monkeypatch.setattr(
        scope, "start_framed_mosaic_item", lambda _p: run(scope, panel_time=10920)
    )
    monkeypatch.setattr(scope, "play_sound", lambda _v: None)
    scope.scheduler_thread_fn()
    assert firmware.methods("next_item_marker")
    assert scope.schedule["state"] == "complete"
    assert scope.framing_status["state"] == "skipped"


def test_new_schedule_clears_the_last_guard_outcome(scope, monkeypatch):
    Firmware(scope, monkeypatch)
    scope.framing_status = {"seq": 1, "state": "skipped"}
    scope.schedule.update(list=[], item_number=1)
    monkeypatch.setattr(scope, "play_sound", lambda _v: None)
    scope.scheduler_thread_fn()
    assert scope.framing_status is None


def test_mismatch_after_the_view_start_does_not_start_a_second_stack(scope, monkeypatch):
    firmware = Firmware(scope, monkeypatch, resets=1)
    real = firmware.receive

    def forget_mosaic(data):
        if data["method"] == "iscope_start_view" and len(view_starts(firmware)) == 1:
            data = {**data, "params": {k: v for k, v in data["params"].items() if k != "mosaic"}}
        return real(data)

    monkeypatch.setattr(scope, "send_message_param_sync", forget_mosaic)
    run(scope, panel_time=10920)
    assert len(firmware.methods("iscope_start_stack")) == 1
    assert scope.framing_status["state"] == "skipped"
    assert "the view start left scale 1 angle 0" in scope.framing_status["note"]


@pytest.mark.parametrize(
    "reply",
    [
        {"method": "get_device_state", "result": "Error: Exceeded allotted wait time"},
        {"result": {}},
        {"result": {"setting": {}}},
        {"result": {"setting": {"mosaic": {"scale": "1.7"}}}},
        {"result": {"setting": {"mosaic": {"scale": True}}}},
        {},
        "garbage",
        OSError("socket gone"),
    ],
)
def test_failed_read_keeps_stacking_and_says_so(scope, monkeypatch, diag, reply):
    firmware = Firmware(scope, monkeypatch, resets=99)
    firmware.read_reply = reply
    run(scope)
    assert len(firmware.methods("iscope_start_stack")) == 1
    assert len(view_starts(firmware)) == 1
    assert action(scope) == "complete"
    assert scope.framing_status["state"] == "unverified"
    assert "scope unreadable" in diag.read_text()


def test_failed_read_after_the_retry_keeps_stacking(scope, monkeypatch):
    firmware = Firmware(scope, monkeypatch, resets=1)

    def fail_reads_after_second_stack(method):
        if method == "iscope_start_stack" and len(firmware.methods(method)) == 2:
            firmware.read_reply = {"result": "Error: Exceeded allotted wait time"}

    firmware.hook = fail_reads_after_second_stack
    run(scope)
    assert scope.framing_status["state"] == "unconfirmed"
    assert "could not be read" in scope.framing_status["note"]
    assert firmware.stack_running is False  # only stopped by the normal end of the item
    assert action(scope) == "complete"


def request_stop(scope):
    """What stop_scheduler leaves behind, including its commands."""
    if scope.schedule["state"] == "working":
        scope.stop_scheduler()


@pytest.mark.parametrize(
    "stop_at,view_starts_expected,stacks_expected",
    [
        ("first_stop_stack", 1, 1),  # as the wrong stack is being stopped
        ("second_view_start", 2, 1),  # as the mosaic is being resent
        ("second_set_stack_type", 2, 1),  # just before stacking again
        ("second_start_stack", 2, 2),  # as the second stack starts
    ],
)
def test_stop_during_the_retry_sends_nothing_new_and_stops(
    scope, monkeypatch, stop_at, view_starts_expected, stacks_expected
):
    firmware = Firmware(scope, monkeypatch, resets=1)
    monkeypatch.setattr(scope, "play_sound", lambda _v: None)
    stopping = []

    def hook(method):
        if stopping:
            return
        hit = {
            "first_stop_stack": method == "iscope_stop_view",
            "second_view_start": method == "iscope_start_view"
            and len(view_starts(firmware)) == 2,
            "second_set_stack_type": method == "set_stack_type"
            and len(firmware.methods(method)) == 2,
            "second_start_stack": method == "iscope_start_stack"
            and len(firmware.methods(method)) == 2,
        }[stop_at]
        if hit:
            stopping.append(True)
            request_stop(scope)

    firmware.hook = hook
    run(scope, panel_time=10920)
    assert len(view_starts(firmware)) == view_starts_expected
    assert len(firmware.methods("iscope_start_stack")) == stacks_expected
    assert scope.schedule["state"] == "stopped"
    assert firmware.stack_running is False
    assert scope.framing_status["state"] == "interrupted"
    assert scope.is_cur_scheduler_item_working is False
    assert firmware.sleeps < 30
    # A view start that may have overtaken the stop gets its own stop_slew.
    if stop_at == "second_view_start":
        assert len(stops(firmware, "AutoGoto")) >= 2


def test_stop_during_the_retry_goto_wait_returns_within_a_second(scope, monkeypatch):
    firmware = Firmware(scope, monkeypatch, resets=1)
    monkeypatch.setattr(scope, "play_sound", lambda _v: None)
    # The first goto completes as usual; the retry's never does before the stop.
    original_sleep = firmware.sleep
    waited = []

    def sleep(seconds):
        if len(view_starts(firmware)) == 2:
            firmware.goto_outcome = None
            waited.append(seconds)
            if len(waited) == 5:
                request_stop(scope)
        original_sleep(seconds)

    monkeypatch.setattr("device.seestar_device.time.sleep", sleep)
    run(scope, panel_time=10920)
    assert len(waited) == 5
    assert len(firmware.methods("iscope_start_stack")) == 1
    assert scope.schedule["state"] == "stopped"
    assert len(stops(firmware, "AutoGoto")) == 2  # stop_scheduler's, and the guard's own


def test_skip_during_the_retry_ends_the_item_without_a_second_stack(scope, monkeypatch):
    firmware = Firmware(scope, monkeypatch, resets=1)

    def hook(method):
        if method == "iscope_start_view" and len(view_starts(firmware)) == 2:
            scope.skip_scheduler_cur_item({})

    firmware.hook = hook
    run(scope, panel_time=10920)
    assert len(firmware.methods("iscope_start_stack")) == 1
    assert scope.framing_status["state"] == "interrupted"
    assert firmware.sleeps < 30


def test_retry_goto_that_never_finishes_is_bounded_and_ends_the_item(scope, monkeypatch):
    firmware = Firmware(scope, monkeypatch, resets=1)
    original_sleep = firmware.sleep

    def sleep(seconds):
        if len(view_starts(firmware)) == 2:
            firmware.goto_outcome = None
        original_sleep(seconds)

    monkeypatch.setattr("device.seestar_device.time.sleep", sleep)
    run(scope, panel_time=10920)
    assert firmware.sleeps <= FRAMING_RETRY_GOTO_TIMEOUT_S + 20
    assert stops(firmware, "AutoGoto")
    assert scope.is_goto() is False  # the next item's goto isn't blocked by this one
    assert scope.framing_status["state"] == "skipped"
    assert "had not finished" in scope.framing_status["note"]


@pytest.mark.parametrize("outcome", ["fail", "cancel"])
def test_retry_goto_failure_ends_the_item(scope, monkeypatch, outcome):
    firmware = Firmware(scope, monkeypatch, resets=1)
    original_sleep = firmware.sleep

    def sleep(seconds):
        if len(view_starts(firmware)) == 2:
            firmware.goto_outcome = outcome
        original_sleep(seconds)

    monkeypatch.setattr("device.seestar_device.time.sleep", sleep)
    run(scope, panel_time=10920)
    assert len(firmware.methods("iscope_start_stack")) == 1
    assert scope.framing_status["state"] == "skipped"
    assert firmware.stack_running is False


def test_first_stack_that_will_not_stop_gets_no_retry(scope, monkeypatch):
    firmware = Firmware(scope, monkeypatch, resets=1)
    real = firmware.receive
    failed = []

    def receive(data):
        reply = real(data)
        if data["method"] == "iscope_stop_view" and not failed:
            failed.append(True)
            return {"error": "busy", "code": 1}
        return reply

    monkeypatch.setattr(scope, "send_message_param_sync", receive)
    run(scope, panel_time=10920)
    assert len(view_starts(firmware)) == 1
    assert scope.framing_status["state"] == "skipped"
    assert "could not be stopped (busy)" in scope.framing_status["note"]


@pytest.mark.parametrize(
    "reported,matches",
    [
        ({"scale": 1.72, "angle": 67.4}, True),
        ({"scale": 1.7}, True),  # no angle in the reply: judged on its scale
        ({"scale": 1.7, "angle": -113.0}, True),  # the same rectangle, turned 180
        ({"scale": 1.0, "angle": 67.0}, False),
        ({"scale": 1.7, "angle": 0.0}, False),  # angle alone is enough
    ],
)
def test_tolerances(scope, monkeypatch, reported, matches):
    firmware = Firmware(scope, monkeypatch)
    firmware.hook = lambda method: setattr(firmware, "mosaic", reported) if (
        method == "get_device_state" and len(firmware.methods("iscope_start_stack")) == 1
    ) else None
    run(scope, panel_time=10920 if not matches else 10)
    assert scope.framing_status["state"] == ("held" if matches else "skipped")


def test_framed_item_at_scale_one_is_left_alone(scope, monkeypatch, diag):
    firmware = Firmware(scope, monkeypatch)
    run(scope, scale=1.0, angle=0.0)
    assert firmware.methods("get_device_state") == []
    assert scope.framing_status is None
    assert not diag.exists()
    assert len(firmware.methods("iscope_start_stack")) == 1


def test_single_frame_item_is_never_read_or_retried(scope, monkeypatch, diag):
    firmware = Firmware(scope, monkeypatch, resets=99)
    monkeypatch.setattr(
        "device.seestar_device.Util.mosaic_next_center_spacing", lambda *_a: [0.1, 0.2]
    )
    scope.mosaic_thread_fn("T", 2.74, 61.3, True, 10, 1, 1, 10, 200, False, "", 1, 5)
    assert firmware.methods("get_device_state") == []
    assert len(firmware.methods("iscope_start_stack")) == 1
    assert scope.framing_status is None
    assert not diag.exists()


def test_diagnostics_are_off_unless_the_app_enables_them(scope, monkeypatch, tmp_path):
    framing_log.disable()
    monkeypatch.chdir(tmp_path)
    Firmware(scope, monkeypatch)
    run(scope)
    assert list(tmp_path.iterdir()) == []


def test_diagnostics_append_and_survive_a_bad_path(tmp_path):
    framing_log.enable(str(tmp_path))
    try:
        framing_log.append("one")
        framing_log.append("two")
        lines = (tmp_path / framing_log.FILE_NAME).read_text().splitlines()
        assert [line.split(" ", 2)[2] for line in lines] == ["one", "two"]
        framing_log.enable(str(tmp_path / "missing" / "dir"))
        framing_log.append("three")  # must not raise
    finally:
        framing_log.disable()


def test_only_the_real_app_turns_the_diagnostics_on():
    root = Path(__file__).resolve().parent.parent
    assert "framing_log.enable(os.getcwd())" in (root / "root_app.py").read_text()
