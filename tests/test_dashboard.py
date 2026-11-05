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
    assert 'hx-get="/1/dashboard/events"' in html
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


def test_live_fragment_shows_the_stage_before_stacking(client, monkeypatch):
    view = {**VIEW, "stage": "AutoGoto", "AutoGoto": {"stage": "slew"}}
    monkeypatch.setattr(
        front_app, "method_sync", lambda method, telescope_id=1, **kw: {"View": view}
    )
    html = client.simulate_get("/1/dashboard/live").text
    assert "AutoGoto (slew)" in html and "gain 200" in html


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


EVENTS = {
    "scheduler": {"Event": "Scheduler", "state": "working"},
    "PlateSolve": {"Event": "PlateSolve", "state": "fail", "error": "solve failed"},
    "AutoFocus": {"Event": "AutoFocus", "state": "complete", "position": 1174},
    "Stack": {"Event": "Stack", "state": "working", "stacked_frame": 6, "dropped_frame": 2},
    "WheelMove": {"Event": "WheelMove", "state": "complete", "position": 2},
    "Sleep": {"Event": "Sleep", "state": "working"},
    "not-an-event": 3,
}


def test_event_results_tolerates_missing_data():
    assert dashboard.event_results(None) == []
    assert dashboard.event_results({"Value": {}}) == []
    assert dashboard.event_results({"Value": {"result": "Offline"}}) == []
    assert len(dashboard.event_results({"Value": {"result": EVENTS}})) == 6


def test_status_cards_use_the_startup_card(client, monkeypatch):
    monkeypatch.setattr(
        front_app, "do_action_device", lambda *a, **kw: {"Value": {"result": EVENTS}}
    )
    html = client.simulate_get("/1/dashboard/events").text
    assert html.count('class="card text-center') == len(dashboard.EVENT_CARDS)
    assert html.count('class="col-6 col-xl-4"') == len(dashboard.EVENT_CARDS)
    for expected in ("FilterWheel", "Filter:</strong> LP", "solve failed", "text-bg-danger"):
        assert expected in html
    for trimmed in ("Sleep", "Scheduler", "Stacked:"):
        assert trimmed not in html


def test_status_cards_when_the_scope_is_offline(client, monkeypatch):
    monkeypatch.setattr(front_app, "do_action_device", lambda *a, **kw: None)
    result = client.simulate_get("/1/dashboard/events")
    assert result.status_code == 200
    assert "No results available" in result.text


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
