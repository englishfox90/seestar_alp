"""The /dashboard page: what it shows, and when it asks the scope for an image."""

import json
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


def test_page_polls_its_fragments_and_is_read_only(client, monkeypatch):
    monkeypatch.setattr(front_app, "get_context", lambda *a: {"root": "/1", "telescope": {}, "telescopes": []})
    html = client.simulate_get("/1/dashboard").text
    assert 'hx-get="/1/dashboard/live"' in html
    assert 'hx-get="/1/dashboard/device"' in html
    assert 'hx-get="/1/dashboard/tracker"' in html
    assert html.index("dash-tracker") < html.index("Latest image")
    assert "every 15s [!document.hidden]" in html
    assert 'hx-sync="this:drop"' in html
    assert 'id="dash-session"' in html
    assert html.index("Latest image") < html.index('id="dash-session"')
    assert 'method="post"' not in html.split('id="dash-live"')[1]


def test_nav_links_to_the_dashboard():
    html = front_app.fetch_template("nav.html").render(root="/1", telescope={})
    assert 'href="/1/dashboard"' in html


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
    events = {**DONE_DARKS, "AutoFocus": {"state": "fail"}}
    tracker = dashboard.tracker_steps(BASE, events)
    assert tracker["steps"][3]["state"] == "warn"
    assert tracker["notes"] == ["focus failed, carried on anyway"]


def test_tracker_ignores_an_old_plate_solve_failure_once_past_the_slew():
    events = {**DONE_DARKS, "PlateSolve": {"state": "fail", "error": "solve failed"}}
    assert _states({}, events)[2] == "done"


def test_tracker_shows_a_failed_goto():
    events = {"AutoGoto": {"state": "fail", "error": "no stars"}}
    status = {"view_state": "cancel", "view_stage": None}
    assert _states(status, events)[2] == "failed"
    assert _detail(status, events) == "no stars"


def test_tracker_while_opening_and_parking():
    assert _states({"phase": "opening", "view_state": None})[:3] == ["done", "active", "pending"]
    parking = _states({"phase": "parking", "view_state": "cancel"}, DONE_DARKS)
    assert parking == ["done"] * 6 + ["active"]


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
