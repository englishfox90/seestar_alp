"""The /dashboard page: what it shows, and when it asks the scope for an image."""

import json
import re
from types import SimpleNamespace

import falcon
import falcon.testing
import numpy as np
import pytest

import front.app as front_app
from roof import dashboard, web as roof_web

VIEW = {
    "state": "working",
    "lapse_ms": 499152,
    "mode": "star",
    "target_ra_dec": [2.745853, 61.358906],
    "target_name": "IC 1805",
    "lp_filter": False,
    "gain": 200,
    "Stack": {
        "state": "working",
        "lapse_ms": 498361,
        "stacked_frame": 6,
        "dropped_frame": 2,
        "Exposure": {"state": "working", "exp_ms": 30000.0},
        "stage": "Exposure",
    },
    "stage": "Stack",
}
SCHEDULE = {
    "state": "working",
    "current_item_id": "b",
    "list": [
        {"action": "set_setting", "params": {}, "schedule_item_id": "a"},
        {
            "action": "start_framed_mosaic",
            "params": {"target_name": "IC 1805", "panel_time_sec": 10800},
            "schedule_item_id": "b",
        },
    ],
}
STATUS = {
    "safe": True,
    "dark": True,
    "sun_deg": -43.0,
    "connected": True,
    "parked": False,
    "enabled": True,
    "dry_run": False,
    "loop_stale": False,
    "headline": "Imaging IC 1805",
    "schedule": "Cygnus.json",
    "sched_state": "working",
    "target": "IC 1805",
    "targets": [
        {"name": "NGC 6960", "kind": "mosaic", "altitude": 45.2, "ok": True},
        {"name": "IC 1805", "kind": "mosaic", "altitude": 60.0, "ok": True},
        {"name": "M 42", "kind": "single", "altitude": 10.0, "ok": False},
    ],
    "log": [],
}


def test_session_summary_while_stacking():
    session = dashboard.session_summary(VIEW, SCHEDULE)
    assert session["active"] and session["target"] == "IC 1805"
    assert session["planned"] == "3h"
    assert session["stack"] == {
        "stacked": 6,
        "dropped": 2,
        "drop_percent": 25,
        "exposure_s": 30.0,
        "integration": "0:03:00",
        "elapsed": "0:08:18",
    }


@pytest.mark.parametrize("view", [None, {}, "Offline", {"state": "cancel", "Stack": {}}])
def test_session_summary_when_not_imaging(view):
    session = dashboard.session_summary(view)
    assert not session["active"]
    assert session["stack"] is None


def test_session_summary_ignores_stack_left_over_after_a_session():
    view = {**VIEW, "state": "cancel"}
    assert dashboard.session_summary(view)["stack"] is None


def test_session_summary_with_no_frames_yet():
    stack = {"stacked_frame": 0, "dropped_frame": 0, "lapse_ms": 0}
    session = dashboard.session_summary({**VIEW, "Stack": stack})
    assert session["stack"]["drop_percent"] is None
    assert session["stack"]["integration"] is None


def test_plan_rows_mark_progress():
    rows = dashboard.plan_rows(STATUS, {"target": "IC 1805"})
    assert [r["progress"] for r in rows] == ["done", "now", "next"]


def test_plan_rows_have_no_progress_when_not_running():
    rows = dashboard.plan_rows({**STATUS, "sched_state": "stopped"}, {})
    assert [r["progress"] for r in rows] == [None, None, None]


def _row(rows, key):
    return next(r for r in rows if r["key"] == key)


@pytest.mark.parametrize(
    "stats, key, value, tone, bar",
    [
        ({"Battery %": 100}, "Battery", "100%", "text-success", 100),
        ({"Battery %": 12}, "Battery", "12%", "text-warning", 12),
        ({"Battery %": 4}, "Battery", "4%", "text-danger", 4),
        ({"Free Storage": "92.8 GB"}, "Free storage", "92.8 GB", "text-success", None),
        ({"Free Storage": "9.5 GB"}, "Free storage", "9.5 GB", "text-warning", None),
        ({"Free Storage": "512.0 MB"}, "Free storage", "512.0 MB", "text-danger", None),
        ({"Free Storage": "1.2 TB"}, "Free storage", "1.2 TB", "text-success", None),
        ({"Free Storage": "Unknown"}, "Free storage", "Unknown", "", None),
        ({"Wi-Fi Signal": "-60 dBm"}, "Wi-Fi signal", "-60 dBm", "text-success", 75),
        ({"Wi-Fi Signal": "-85 dBm"}, "Wi-Fi signal", "-85 dBm", "text-danger", 12),
        (
            {"Wi-Fi Signal": "Unavailable in AP mode."},
            "Wi-Fi signal",
            "Unavailable in AP mode.",
            "",
            None,
        ),
        ({"Heater?": False}, "Dew heater", "Off", "", None),
        ({"Temperature Sensor": 32.610001}, "Scope temperature", "32.6 °C", "", None),
    ],
)
def test_device_rows(stats, key, value, tone, bar):
    row = _row(dashboard.device_rows(stats), key)
    assert (row["value"], row["tone"], row["bar"]) == (value, tone, bar)


def test_device_rows_leave_out_what_does_not_help_during_a_session():
    stats = {
        "Battery %": 100,
        "Battery Temp": 16,
        "Focal Position": 1174,
        "Balance Sensor (angle)": 1.2,
        "Compass Sensor (direction)": 181,
        "Firmware Version": "9.75",
    }
    assert [r["key"] for r in dashboard.device_rows(stats)] == ["Battery"]


def test_device_rows_empty_when_offline():
    assert dashboard.device_rows({}) == []


class FakeComm:
    def __init__(self):
        self.connected = True
        self.frame = 0
        self.raw = None
        self.StarProcessors = []

    def is_connected(self):
        return self.connected

    def is_streaming(self):
        return False

    def received_frame(self):
        return self.frame

    def get_unprocessed_image(self):
        return self.raw, 1080, 1920


class FakeImager:
    def __init__(self, view):
        self.comm = FakeComm()
        self.device = SimpleNamespace(view_state=view)
        self.requests = 0

    def request_stacked_image(self):
        self.requests += 1

    def receive(self):
        self.comm.frame += 1
        self.comm.raw = np.full((192, 108, 3), 128, dtype=np.uint16)


def _view(stacked, dropped=0, **extra):
    stack = {**VIEW["Stack"], "stacked_frame": stacked, "dropped_frame": dropped}
    return {**VIEW, "Stack": stack, **extra}


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def store():
    clock = Clock()
    return dashboard.ImageStore(clock=clock), clock


def test_no_request_unless_the_browser_asks(store):
    images, _ = store
    imager = FakeImager(_view(1))
    assert images.latest(imager) is None
    assert imager.requests == 0


def test_fetch_requests_once_per_new_stacked_frame(store):
    images, clock = store
    imager = FakeImager(_view(1))
    images.latest(imager, fetch=True)
    assert imager.requests == 1
    imager.receive()

    clock.now += 60
    assert images.latest(imager, fetch=True)["stacked"] == 1
    assert imager.requests == 1, "same frame count, nothing new to fetch"

    imager.device.view_state = _view(1, dropped=5)
    clock.now += 60
    images.latest(imager, fetch=True)
    assert imager.requests == 1, "a dropped frame does not change the stack"

    imager.device.view_state = _view(2)
    clock.now += 60
    images.latest(imager, fetch=True)
    assert imager.requests == 2


def test_fetches_are_rate_limited(store):
    images, clock = store
    imager = FakeImager(_view(1))
    images.latest(imager, fetch=True)
    imager.receive()
    imager.device.view_state = _view(2)
    clock.now += dashboard.MIN_FETCH_INTERVAL_S - 1
    images.latest(imager, fetch=True)
    assert imager.requests == 1
    clock.now += 1
    images.latest(imager, fetch=True)
    assert imager.requests == 2


def test_unanswered_request_is_retried(store):
    images, clock = store
    imager = FakeImager(_view(1))
    images.latest(imager, fetch=True)
    clock.now += dashboard.MIN_FETCH_INTERVAL_S
    images.latest(imager, fetch=True)
    assert imager.requests == 2


@pytest.mark.parametrize(
    "view, connected",
    [
        (_view(0), True),
        (_view(3), False),
        (_view(3, state="cancel"), True),
        (_view(3, stage="AutoGoto"), True),
        ({}, True),
    ],
)
def test_no_request_when_there_is_nothing_to_fetch(store, view, connected):
    images, _ = store
    imager = FakeImager(view)
    imager.comm.connected = connected
    images.latest(imager, fetch=True)
    assert imager.requests == 0


def test_last_image_survives_the_end_of_the_session(store):
    images, clock = store
    imager = FakeImager(_view(7))
    images.latest(imager, fetch=True)
    imager.receive()
    first = images.latest(imager)
    imager.device.view_state = {"state": "cancel"}
    clock.now += 60
    again = images.latest(imager, fetch=True)
    assert again is first
    assert again["target"] == "IC 1805" and again["stacked"] == 7


def test_a_stale_request_does_not_label_a_later_image(store):
    """A browser asked during SH2-157, left, and came back after IC 1805 had replaced it."""
    images, clock = store
    imager = FakeImager(_view(41, target_name="SH2-157"))
    images.latest(imager, fetch=True)
    clock.now += dashboard.PENDING_MAX_AGE_S + 1
    imager.device.view_state = _view(247, target_name="IC 1805")
    imager.receive()
    image = images.latest(imager)
    assert image["target"] == "IC 1805" and image["stacked"] == 247


def test_capture_time_is_when_the_image_arrived_not_when_it_was_seen(store):
    images, clock = store
    imager = FakeImager(_view(7))
    images.latest(imager, fetch=True)
    imager.receive()
    imager.comm.received_at = lambda: 500.0
    clock.now += 3600
    assert images.latest(imager)["captured_at"] == 500.0


def test_a_stale_request_with_no_stacking_view_leaves_the_image_unnamed(store):
    images, clock = store
    imager = FakeImager(_view(41, target_name="SH2-157"))
    images.latest(imager, fetch=True)
    clock.now += dashboard.PENDING_MAX_AGE_S + 1
    imager.device.view_state = {"state": "idle", "target_name": "SH2-157"}
    imager.receive()
    image = images.latest(imager)
    assert image["target"] is None and image["stacked"] is None
    assert dashboard.download_name(image).startswith("seestar_")


def test_a_request_does_not_label_an_image_that_arrived_before_it(store):
    images, _ = store
    imager = FakeImager(_view(7, target_name="IC 1805"))
    imager.comm.received_at = lambda: 10.0
    imager.receive()
    images.latest(imager, fetch=True)
    imager.device.view_state = {"state": "idle"}
    imager.comm.frame += 1
    image = images.latest(imager)
    assert image["target"] is None


def test_caption_comes_from_the_request_not_the_next_target(store):
    images, _ = store
    imager = FakeImager(_view(7))
    images.latest(imager, fetch=True)
    imager.device.view_state = _view(0, target_name="M 42")
    imager.receive()
    assert images.latest(imager)["target"] == "IC 1805"


def test_image_is_only_stretched_once_per_frame(store):
    images, _ = store
    imager = FakeImager(_view(1))
    calls = []

    class Stretch:
        def process(self, image):
            calls.append(1)
            return image.astype(float)

    imager.comm.StarProcessors = [Stretch()]
    imager.receive()
    images.latest(imager)
    images.latest(imager)
    assert len(calls) == 1


def test_large_image_gets_a_smaller_preview(store):
    images, _ = store
    imager = FakeImager(_view(1))
    imager.receive()
    imager.comm.raw = np.random.randint(0, 255, (1920, 1080, 3), dtype=np.uint16)
    image = images.latest(imager)
    assert image["preview"][:2] == b"\xff\xd8"
    assert len(image["preview"]) < len(image["full"])


@pytest.mark.parametrize("value, shown", [(6.24, 6.2), (0.0, None), (float("nan"), None), (None, None)])
def test_snr_hides_values_that_mean_nothing(monkeypatch, value, shown):
    from device.analysis.snr_analysis import SNRAnalysis

    monkeypatch.setattr(SNRAnalysis, "analyze", lambda self, raw: value)
    assert dashboard._snr(np.zeros((2, 2))) == shown


def test_download_name_is_safe():
    image = {"target": 'IC "1805"/../x', "stacked": 6, "captured_at": 1_800_000_000}
    name = dashboard.download_name(image)
    assert name.startswith("IC_1805_x_") and name.endswith("_6frames.jpg")
    assert dashboard.download_name({"target": None, "stacked": None, "captured_at": 0}).startswith(
        "seestar_"
    )


class _FakeAutomation:
    def status(self):
        return STATUS

    def start(self):
        pass


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(roof_web, "get_automation", lambda: _FakeAutomation())
    monkeypatch.setattr(dashboard, "get_automation", lambda: _FakeAutomation())
    monkeypatch.setattr(dashboard, "_stores", {})
    monkeypatch.setattr(
        front_app, "method_sync", lambda method, telescope_id=1, **kw: {"View": VIEW}
    )
    monkeypatch.setattr(
        front_app, "do_action_device", lambda *a, **kw: {"Value": SCHEDULE}
    )
    monkeypatch.setattr(
        front_app,
        "get_device_state",
        lambda telescope_id: {"Battery %": 100, "Mount Mode": "<b>Equatorial</b>"},
    )
    app = falcon.App()
    roof_web.install(app)
    return falcon.testing.TestClient(app)


def _page(client, monkeypatch):
    monkeypatch.setattr(front_app, "get_context", lambda *a: {"root": "/1", "telescope": {}, "telescopes": []})
    return client.simulate_get("/1/dashboard").content.decode()


def test_page_polls_its_state_and_only_posts_stop(client, monkeypatch):
    html = _page(client, monkeypatch)
    assert 'hx-get="/1/tonight/state"' in html
    assert 'hx-get="/1/tonight/focus"' in html
    assert 'hx-get="/1/tonight/hardware"' in html
    assert "every 5s [!document.hidden]" in html
    assert 'hx-sync="this:drop"' in html
    assert "no-htmx-fx" in html
    # Read-only apart from one route: Stop & park.
    assert re.findall(r'<form[^>]*method="post"[^>]*action="([^"]+)"', html) == ["/1/roof/stop"]
    assert html.lower().count("<form") == 1
    assert "hx-post" not in html


def test_nav_calls_the_dashboard_route_tonight():
    html = front_app.fetch_template("nav.html").render(root="/1", telescope={})
    assert '<a class="ssc-widenav__item" href="/1/dashboard">Tonight</a>' in html
    assert "Dashboard" not in html


def test_live_fragment_shows_the_session(client):
    html = client.simulate_get("/1/dashboard/live").text
    for expected in (
        "Imaging IC 1805",
        "Roof safe",
        "0:03:00",
        "6 stacked, 2 dropped (25%)",
        "30 s, gain 200",
        "0:08:18 of 3h",
        "Too low",
        "Now",
    ):
        assert expected in html
    assert "Stage" not in html, "stacking is what the frame counts already say"


@pytest.mark.parametrize(
    "scale, expected", [(1.7, "Mosaic 1.7x"), (1.0, "Single frame")]
)
def test_live_fragment_shows_framing(client, monkeypatch, scale, expected):
    status = {**STATUS, "mosaic_scale": scale}
    automation = SimpleNamespace(status=lambda: status)
    monkeypatch.setattr(dashboard, "get_automation", lambda: automation)
    assert expected in client.simulate_get("/1/dashboard/live").text


def test_live_fragment_before_stacking_still_shows_the_setup(client, monkeypatch):
    view = {**VIEW, "stage": "AutoGoto", "AutoGoto": {"stage": "slew"}}
    monkeypatch.setattr(
        front_app, "method_sync", lambda method, telescope_id=1, **kw: {"View": view}
    )
    html = client.simulate_get("/1/dashboard/live").text
    assert "IC 1805" in html and "gain 200" in html


def test_session_and_plan_arrive_out_of_band(client):
    html = client.simulate_get("/1/dashboard/live").text
    assert '<div id="dash-session" hx-swap-oob="innerHTML">' in html
    assert html.index("Roof safe") < html.index("dash-session")
    assert "Passed" in html


def test_live_fragment_when_the_scope_is_offline(client, monkeypatch):
    monkeypatch.setattr(front_app, "method_sync", lambda *a, **kw: None)
    monkeypatch.setattr(front_app, "do_action_device", lambda *a, **kw: None)
    result = client.simulate_get("/1/dashboard/live")
    assert result.status_code == 200
    assert "Not imaging right now" in result.text


def test_live_fragment_shows_alarms_and_escapes_scope_text(client, monkeypatch):
    status = {**STATUS, "alarm": "<script>x</script>", "loop_stale": True}
    automation = SimpleNamespace(status=lambda: status)
    monkeypatch.setattr(dashboard, "get_automation", lambda: automation)
    html = client.simulate_get("/1/dashboard/live").text
    assert "&lt;script&gt;" in html and "<script>x" not in html
    assert "stopped reporting" in html


def test_device_fragment_escapes_values(client):
    html = client.simulate_get("/1/dashboard/device").text
    assert "100%" in html and "&lt;b&gt;Equatorial" in html


def test_device_fragment_keeps_last_readings_when_the_scope_does_not_answer(
    client, monkeypatch
):
    monkeypatch.setattr(
        front_app, "get_device_state", lambda telescope_id: {"Info": "Unable to get stats."}
    )
    result = client.simulate_get("/1/dashboard/device")
    assert result.status_code == 204
    assert result.text == ""


def test_device_fragment_says_offline_when_it_is(client, monkeypatch):
    monkeypatch.setattr(front_app, "get_device_state", lambda telescope_id: {})
    assert "currently offline" in client.simulate_get("/1/dashboard/device").text


BASE = {
    "safe": True,
    "dark": True,
    "sun_deg": -40.0,
    "sun_max_deg": -18.0,
    "connected": True,
    "parked": False,
    "phase": "imaging",
    "sched_state": "working",
    "view_state": "working",
    "view_stage": "Stack",
    "target": "IC 1805",
}


def _states(status, events=None):
    rows = dashboard.tracker_steps({**BASE, **status}, events)["steps"]
    return [r["state"] for r in rows]


def _detail(status, events=None):
    return dashboard.tracker_steps({**BASE, **status}, events)["detail"]


DONE_DARKS = {"DarkLibrary": {"state": "complete"}}


@pytest.mark.parametrize(
    "stage, expected",
    [
        ("AutoGoto", ["done", "done", "active", "pending", "pending", "pending", "pending"]),
        ("AutoFocus", ["done", "done", "done", "active", "pending", "pending", "pending"]),
        ("Stack", ["done", "done", "done", "done", "done", "active", "pending"]),
    ],
)
def test_tracker_follows_the_stage(stage, expected):
    assert _states({"view_stage": stage}, DONE_DARKS) == expected


def test_tracker_shows_dark_frames_as_their_own_step():
    events = {"DarkLibrary": {"state": "working", "percent": 42.4}}
    assert _states({"view_stage": "AutoFocus"}, events)[3:6] == ["done", "active", "pending"]
    assert _detail({}, events) == "Taking dark frames, 42%"


def test_tracker_marks_darks_skipped_when_none_ran():
    assert _states({})[4] == "skipped"
    assert _states({}, DONE_DARKS)[4] == "done"


def test_tracker_flags_a_failed_focus_the_scope_carried_on_from():
    events = {**DONE_DARKS, "AutoFocus": {"state": "fail", "error": "no star is detected"}}
    tracker = dashboard.tracker_steps(BASE, events)
    assert tracker["steps"][3]["state"] == "warn"
    assert tracker["notes"] == [
        {"text": "Last focus failed: no star is detected", "problem": True}
    ]


def test_tracker_keeps_going_but_reports_a_plate_solve_failure():
    events = {**DONE_DARKS, "PlateSolve": {"state": "fail", "error": "solve failed"}}
    tracker = dashboard.tracker_steps(BASE, events)
    assert tracker["steps"][2]["state"] == "done"
    assert tracker["notes"] == [{"text": "Last plate solve failed: solve failed", "problem": True}]


def test_tracker_notes_list_problems_before_details():
    events = {
        "FocuserMove": {"state": "complete", "position": 1230},
        "AutoGoto": {"state": "complete"},
        "PlateSolve": {"state": "complete"},
        "DarkLibrary": {"state": "fail"},
    }
    assert dashboard.tracker_notes(events) == [
        {"text": "Last dark frames failed", "problem": True},
        {"text": "Focus position 1230", "problem": False},
    ]


def test_tracker_notes_are_empty_with_nothing_to_say():
    assert dashboard.tracker_notes({}) == []
    assert dashboard.tracker_notes({"AutoFocus": {"state": "complete"}}) == []


def test_tracker_note_text_is_escaped(client, monkeypatch):
    events = {"PlateSolve": {"state": "fail", "error": "<script>x</script>"}}
    monkeypatch.setattr(
        front_app, "do_action_device", lambda *a, **kw: {"Value": {"result": events}}
    )
    monkeypatch.setattr(dashboard, "get_automation", lambda: SimpleNamespace(status=lambda: BASE))
    html = client.simulate_get("/1/dashboard/tracker").text
    assert "&lt;script&gt;" in html and "<script>x" not in html
    assert "is-problem" in html


def test_tracker_does_not_tick_steps_a_stop_never_reached():
    status = {"phase": "parking", "view_state": "cancel"}
    assert _states(status, DONE_DARKS) == ["done", "done"] + ["pending"] * 4 + ["active"]


def test_tracker_ignores_a_stage_left_over_after_parking():
    status = {"parked": True, "phase": "idle", "view_stage": "AutoFocus", "sched_state": "working"}
    assert _states(status) == ["done"] + ["pending"] * 6
    assert _detail(status) == "Parked, ready to start"


def test_tracker_trusts_the_stage_during_the_first_goto_of_a_session():
    # The firmware reports the mount closed until the first goto.
    status = {"parked": True, "phase": "imaging", "view_stage": "AutoGoto"}
    assert _states(status)[2] == "active"


def test_tracker_shows_a_failed_goto():
    events = {"AutoGoto": {"state": "fail", "error": "no stars"}}
    status = {"view_state": "cancel", "view_stage": None}
    assert _states(status, events)[2] == "failed"
    assert _detail(status, events) == "Goto failed"
    assert dashboard.tracker_notes(events)[0]["text"] == "Last goto failed: no stars"


def test_tracker_while_opening_and_parking():
    assert _states({"phase": "opening", "view_state": None})[:3] == ["done", "active", "pending"]
    assert _states({"phase": "parking", "view_state": "cancel"})[-1] == "active"


def test_tracker_when_the_roof_is_unsafe():
    status = {"safe": False, "parked": True, "view_state": "cancel", "sched_state": "stopped"}
    assert _states(status)[:2] == ["blocked", "pending"]
    assert "roof" in _detail(status)


def test_tracker_waiting_for_dark():
    status = {
        "dark": False,
        "sun_deg": 12.0,
        "parked": True,
        "view_state": "cancel",
        "sched_state": "stopped",
    }
    assert _states(status)[0] == "active"
    assert "sun 12" in _detail(status)


def test_tracker_when_parked_and_ready():
    status = {"parked": True, "view_state": "cancel", "sched_state": "stopped"}
    assert _states(status) == ["done"] + ["pending"] * 6
    assert _detail(status) == "Parked, ready to start"
    assert "Held" in _detail({**status, "held": True})


def test_tracker_when_the_night_is_finished():
    status = {"parked": True, "completed": True, "view_state": "cancel", "sched_state": "stopped"}
    assert _states(status, DONE_DARKS) == ["done"] * 7
    assert _detail(status, DONE_DARKS) == "Schedule finished and parked"


def test_tracker_when_offline():
    status = {"connected": False, "view_state": None, "sched_state": None, "parked": None}
    assert _detail(status) == "Scope offline"


def test_tracker_marks_the_current_step_failed_on_an_alarm():
    assert _states({"alarm": "Park failed"}, DONE_DARKS)[5] == "failed"


def test_tracker_detail_shows_time_left():
    events = {"scheduler": {"cur_scheduler_item": {"item_remaining_time_s": 3660}}}
    assert _detail({}, events) == "Stacking IC 1805, 1h 1m left"


def test_tracker_fragment_never_asks_the_scope(client, monkeypatch):
    calls = []

    def action(name, *a, **kw):
        calls.append(name)
        return {"Value": {"result": DONE_DARKS}}

    monkeypatch.setattr(front_app, "do_action_device", action)
    monkeypatch.setattr(dashboard, "get_automation", lambda: SimpleNamespace(status=lambda: BASE))
    html = client.simulate_get("/1/dashboard/tracker").text
    assert calls == ["get_event_state"]
    assert html.count('class="dash-step') == 7
    assert 'aria-current="step"' in html
    assert "Stacking IC 1805" in html


def test_event_map_tolerates_missing_data():
    assert dashboard.event_map(None) == {}
    assert dashboard.event_map({"Value": {"result": "Offline"}}) == {}


@pytest.mark.parametrize("position, name", [(0, "Dark"), (1, "IR cut"), (2, "LP"), (None, None)])
def test_filter_name(position, name):
    assert dashboard.filter_name({"WheelMove": {"position": position}}) == name


def test_session_panel_shows_the_filter(client, monkeypatch):
    monkeypatch.setattr(
        front_app,
        "do_action_device",
        lambda *a, **kw: {"Value": {**SCHEDULE, "result": {"WheelMove": {"position": 2}}}},
    )
    assert "LP" in client.simulate_get("/1/dashboard/live").text


def test_image_endpoints_without_an_image(client, monkeypatch):
    monkeypatch.setattr(dashboard, "_imager", lambda telescope_id: None)
    assert json.loads(client.simulate_get("/1/dashboard/image.json").text) == {"frame": None}
    assert client.simulate_get("/1/dashboard/image.jpg").status_code == 404


def test_image_endpoints_serve_preview_and_download(client, monkeypatch):
    imager = FakeImager(_view(6))
    imager.receive()
    monkeypatch.setattr(dashboard, "_imager", lambda telescope_id: imager)

    meta = json.loads(client.simulate_get("/1/dashboard/image.json").text)
    assert meta["frame"] == 1 and meta["target"] == "IC 1805" and meta["stacked"] == 6

    preview = client.simulate_get("/1/dashboard/image.jpg")
    assert preview.headers["content-type"] == "image/jpeg"
    assert "content-disposition" not in preview.headers

    full = client.simulate_get("/1/dashboard/image.jpg", query_string="full=true")
    assert "attachment" in full.headers["content-disposition"]
    assert "IC_1805" in full.headers["content-disposition"]


def test_dashboard_without_a_device_number_goes_to_the_scope(client):
    result = client.simulate_get("/dashboard")
    assert result.status_code == 302
    assert result.headers["location"].endswith("/dashboard")


# ---- pier camera ------------------------------------------------------------


def _jpeg_bytes(width=2048, height=2048):
    import cv2

    ok, data = cv2.imencode(".jpg", np.zeros((height, width, 3), np.uint8))
    assert ok
    return data.tobytes()


class _Reply:
    def __init__(self, content=b"", headers=None, body=None):
        self.content, self.headers, self._body = content, headers or {}, body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


@pytest.fixture
def sentinel(monkeypatch):
    state = SimpleNamespace(calls=0, up=True, tag="one", age=20, content=_jpeg_bytes())

    def fake_get(url, timeout):
        state.calls += 1
        if not state.up:
            raise OSError("down")
        if url == "image":
            return _Reply(state.content, {"ETag": state.tag})
        return _Reply(body={"image_age_seconds": state.age})

    monkeypatch.setattr(dashboard.requests, "get", fake_get)
    return state


def test_pier_image_is_shrunk_and_the_original_kept(sentinel):
    import cv2

    store = dashboard.PierStore(clock=lambda: 1000.0)
    image = store.latest("image", "status")
    preview = cv2.imdecode(np.frombuffer(image["preview"], np.uint8), cv2.IMREAD_COLOR)
    assert preview.shape[1] == dashboard.PIER_PREVIEW_WIDTH
    assert image["full"] == sentinel.content
    assert image["captured_at"] == 980.0


def test_pier_fetches_at_most_once_per_interval(sentinel):
    now = [1000.0]
    store = dashboard.PierStore(clock=lambda: now[0])
    store.latest("image", "status")
    calls = sentinel.calls
    now[0] += dashboard.PIER_FETCH_INTERVAL_S - 1
    store.latest("image", "status")
    assert sentinel.calls == calls
    now[0] += 1
    store.latest("image", "status")
    assert sentinel.calls > calls


def test_pier_keeps_the_last_image_and_says_sentinel_is_down(sentinel):
    now = [1000.0]
    store = dashboard.PierStore(clock=lambda: now[0])
    store.latest("image", "status")
    sentinel.up = False
    now[0] += dashboard.PIER_FETCH_INTERVAL_S
    meta = store.meta(store.latest("image", "status"))
    assert meta["available"] and not meta["online"] and not meta["stale"]


def test_pier_reports_an_old_image_as_stale(sentinel):
    sentinel.age = dashboard.PIER_STALE_S + 1
    store = dashboard.PierStore(clock=lambda: 1000.0)
    assert store.meta(store.latest("image", "status"))["stale"] is True


def test_pier_with_no_image_is_unavailable(sentinel):
    sentinel.up = False
    store = dashboard.PierStore(clock=lambda: 1000.0)
    meta = store.meta(store.latest("image", "status"))
    assert meta == {"available": False, "online": False}


def test_pier_endpoints(client, sentinel, monkeypatch):
    monkeypatch.setattr(dashboard, "_pier_store", dashboard.PierStore(clock=lambda: 1000.0))
    monkeypatch.setattr(
        dashboard.roof_config,
        "load",
        lambda: SimpleNamespace(pier_image_url="image", pier_status_url="status"),
    )
    meta = client.simulate_get("/1/dashboard/pier.json").json
    assert meta["available"] and meta["frame"] == "one"
    preview = client.simulate_get("/1/dashboard/pier.jpg")
    assert preview.headers["content-type"] == "image/jpeg"
    full = client.simulate_get("/1/dashboard/pier.jpg", query_string="full=true")
    assert full.content == sentinel.content


def test_no_image_means_no_panel(client, monkeypatch):
    html = _page(client, monkeypatch)
    assert 'id="t-stack" data-name="stack" data-role="thumb" hidden' in html
    assert 'id="t-pier" data-name="pier" data-role="thumb" hidden' in html


def test_image_fetch_follows_the_loop_when_the_devices_view_is_stale(client, monkeypatch):
    # Tonight doesn't poll get_view_state, so the device's copy stays empty.
    imager = FakeImager({})
    status = {**STATUS, "view_state": "working", "view_stage": "Stack", "view_target": "IC 1805", "stacked": 3, "dropped": 0}
    automation = SimpleNamespace(status=lambda: status)
    monkeypatch.setattr(dashboard, "get_automation", lambda: automation)
    monkeypatch.setattr(dashboard, "_imager", lambda telescope_id: imager)
    monkeypatch.setattr(dashboard.roof_config, "load", lambda: SimpleNamespace(device_num=1))
    client.simulate_get("/1/dashboard/image.json")
    assert imager.requests == 1
    client.simulate_get("/1/dashboard/image.json")
    assert imager.requests == 1, "the existing rate limit still applies"


@pytest.mark.parametrize(
    "status",
    [{"stacked": 3, "loop_stale": True}, {"view_state": "working"}],
)
def test_cached_view_gives_way_when_the_loop_cannot_say(monkeypatch, status):
    monkeypatch.setattr(dashboard.roof_config, "load", lambda: SimpleNamespace(device_num=1))
    assert dashboard.cached_view(1, status) is None


def test_cached_view_is_only_for_the_automations_scope(monkeypatch):
    monkeypatch.setattr(dashboard.roof_config, "load", lambda: SimpleNamespace(device_num=1))
    assert dashboard.cached_view(2, {"stacked": 3}) is None


def test_image_times_come_from_the_observatory(client, monkeypatch):
    import pytz

    monkeypatch.setattr(front_app, "site_timezone", lambda: pytz.timezone("Asia/Tokyo"))
    # 2026-11-05 13:14 UTC is 22:14 in Tokyo.
    assert dashboard.site_clock(1793970840) == "22:14"
    image = {"frame": 1, "target": "x", "stacked": 1, "captured_at": 1793970840, "snr": None}
    assert dashboard._image_meta(image)["captured_clock"] == "22:14"


def test_a_frame_on_its_way_is_flagged_for_a_quick_look(store):
    # The page looks again within seconds while a frame it asked for is on its way.
    images, _ = store
    imager = FakeImager(_view(3))
    images.latest(imager, fetch=True)
    assert imager.requests == 1 and images.pending()
    imager.receive()
    images.latest(imager)
    assert not images.pending()


def test_an_unanswered_request_stops_the_quick_look(store):
    images, clock = store
    imager = FakeImager(_view(3))
    images.latest(imager, fetch=True)
    clock.now += dashboard.PENDING_QUICK_S + 1
    assert not images.pending()


def test_image_json_says_when_a_frame_is_pending(client, monkeypatch):
    imager = FakeImager(_view(6))
    monkeypatch.setattr(dashboard, "_imager", lambda telescope_id: imager)
    meta = json.loads(client.simulate_get("/1/dashboard/image.json").text)
    assert imager.requests == 1
    assert meta == {"frame": None, "pending": True}


def test_another_targets_image_is_held_back_while_stacking(client, monkeypatch):
    from roof import tonight

    old = {"frame": 7, "target": "M 31", "stacked": 90, "captured_at": 0, "captured_clock": "21:02", "snr": None}
    monkeypatch.setattr(tonight, "_first_images", lambda telescope_id: {"stack": old, "pier": None})
    monkeypatch.setattr(tonight, "stack_waiting", lambda state, stack: True)
    html = _page(client, monkeypatch)
    assert 'data-waiting="1"' in html
    assert 'id="t-stack-placeholder"' in html
    assert "image.jpg?v=7" not in html
    assert 'id="t-stack" data-name="stack" data-role="front"' in html
