"""The autofocus curve: what is kept from the scope's events, and how it is drawn."""

import blinker
import falcon
import falcon.testing
import pytest

import front.app as front_app
from roof import dashboard, focus, web as roof_web

# Taken from a real run on the S30 Pro: 6 steps failed to measure and the fit is all zeros.
BAD_FIT = {
    "curve": [0.0, 0.0, 0.0],
    "points": [
        [1310, 19.409464],
        [1300, 0.0],
        [1290, 0.0],
        [1240, 9.089856],
        [1230, 7.69312],
        [1170, 2.311561],
        [1150, 3.134645],
        [1179, 2.290634],
    ],
}
# The same, from a clean run: a real fit, and 1176 is where the focuser ended up.
GOOD_FIT = {
    "curve": [0.001116, -2.609771, 1528.618756],
    "points": [
        [1310, 18.897476],
        [1240, 8.626658],
        [1180, 2.374087],
        [1170, 2.400235],
        [1150, 3.806868],
        [1176, 2.312755],
    ],
}


def _event(state, **extra):
    return {"Event": "AutoFocus", "state": state, **extra}


class _Clock:
    now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture
def log(clock):
    return focus.FocusLog(clock=clock)


def _finish(log, result=GOOD_FIT):
    log.on_event(_event("start"))
    log.on_event(_event("working", result=result))
    log.on_event(_event("complete", result={"last_point": [0, 0.0]}))
    log.on_event(_event("complete", lapse_ms=63762, route=["View"]))


def test_nothing_to_show_before_any_autofocus(log):
    assert log.current() is None


def test_the_points_survive_the_complete_event_that_has_none(log):
    _finish(log)
    run = log.current()
    assert run["state"] == "complete"
    assert run["lapse_ms"] == 63762
    assert run["points"][-1] == (1176, 2.312755)


def test_a_new_run_clears_the_last_one(log):
    _finish(log)
    log.on_event(_event("start"))
    run = log.current()
    assert run["state"] == "working" and run["points"] == []


def test_a_run_already_under_way_is_picked_up_without_its_start(log):
    log.on_event(_event("working", result={"points": [[1310, 18.9]]}))
    assert log.current()["points"] == [(1310, 18.9)]


def test_a_finished_run_shows_for_a_while_then_gives_the_image_back(log, clock):
    _finish(log)
    clock.now += focus.LINGER_S
    assert log.current() is not None
    clock.now += 1
    assert log.current() is None


def test_an_abandoned_run_does_not_hide_the_image_forever(log, clock):
    log.on_event(_event("start"))
    log.on_event(_event("working", result={"points": [[1310, 18.9]]}))
    clock.now += focus.RUN_TIMEOUT_S + 1
    assert log.current() is None


def test_a_cancel_after_stopping_does_not_rewrite_how_the_run_ended(log, clock):
    _finish(log)
    finished_at = log.current()["finished_at"]
    clock.now += 5
    log.on_event(_event("cancel"))
    run = log.current()
    assert run["state"] == "complete" and run["finished_at"] == finished_at


def test_a_failed_run_keeps_the_scopes_error(log):
    log.on_event(_event("start"))
    log.on_event(_event("fail", error="no star is detected"))
    assert focus.chart(log.current())["summary"] == "Autofocus failed: no star is detected"


def test_other_events_and_rubbish_are_ignored(log):
    log.on_event({"Event": "FocuserMove", "state": "working", "position": 1184})
    log.on_event("not an event")
    log.on_event(None)
    assert log.current() is None


def test_rubbish_points_are_dropped(log):
    points = [[1310, 18.9], "x", [1300], [1290, None], [1280, float("nan")], [1270, 5.0]]
    log.on_event(_event("working", result={"points": points}))
    assert log.current()["points"] == [(1310, 18.9), (1270, 5.0)]


def test_a_bad_event_cannot_break_the_thread_reading_the_scope(log, monkeypatch):
    monkeypatch.setattr(log, "_record", lambda event: 1 / 0)
    log.on_event(_event("start"))


def _run(result, state="working", lapse_ms=None):
    log = focus.FocusLog()
    log.on_event(_event("working", result=result))
    run = log.current()
    run.update(state=state, lapse_ms=lapse_ms)
    return run


def test_chart_while_measuring_has_no_settled_position():
    chart = focus.chart(_run({"points": [[1310, 18.9], [1300, 17.0]]}))
    assert chart["final"] is None
    assert chart["summary"] == "Measuring: step 1300, metric 17.00"
    assert len(chart["readings"]) == 2 and len(chart["lines"]) == 1


def test_chart_of_a_clean_run_marks_where_focus_settled():
    chart = focus.chart(_run(GOOD_FIT, "complete", 63762))
    assert chart["final"]["step"] == 1176
    assert chart["summary"] == "Focus settled at step 1176, metric 2.31, took 64 s"
    assert chart["fit"] and chart["notes"] == []
    assert len(chart["readings"]) == 5  # The settled point is drawn apart from the sweep.


def test_chart_puts_lower_metrics_lower_and_higher_steps_right():
    chart = focus.chart(_run(GOOD_FIT, "complete"))
    readings = {r["step"]: r for r in chart["readings"]}
    assert readings[1310]["x"] > readings[1150]["x"]
    assert readings[1170]["y"] > readings[1310]["y"]


def test_chart_stays_inside_the_plot():
    chart = focus.chart(_run(GOOD_FIT, "complete"))
    for point in chart["readings"] + [chart["final"]]:
        assert chart["left"] <= point["x"] <= chart["right"]
        assert chart["top"] <= point["y"] <= chart["bottom"]
    for line in chart["fit"]:
        for pair in line.split():
            assert chart["top"] <= float(pair.split(",")[1]) <= chart["bottom"]


def test_chart_leaves_gaps_for_missing_readings_and_flags_a_bad_fit():
    chart = focus.chart(_run(BAD_FIT, "complete"))
    assert [m["step"] for m in chart["missing"]] == [1300, 1290]
    assert chart["fit"] == []
    # 1310 stands alone, then the sweep picks up again at 1240.
    assert len(chart["lines"]) == 1 and chart["lines"][0].count(",") == 4
    assert [n["text"] for n in chart["notes"]] == [
        "2 steps gave no reading",
        "The scope reported no usable curve fit",
    ]
    assert all(r["metric"] > 0 for r in chart["readings"])


def test_chart_before_the_first_point():
    chart = focus.chart({**_run({"points": []}), "curve": None})
    assert chart["summary"] == "Starting autofocus"
    assert chart["readings"] == [] and chart["x_ticks"] == []


def test_chart_of_a_single_point_does_not_divide_by_zero():
    assert len(focus.chart(_run({"points": [[1310, 18.9]]}))["readings"]) == 1


@pytest.mark.parametrize("points", [[[1310, 0.0]], [[1310, 0.0], [1300, 0.0]]])
def test_chart_with_only_missing_readings(points):
    chart = focus.chart(_run({"points": points}))
    assert len(chart["missing"]) == len(points) and chart["y_ticks"]


def test_listen_records_events_sent_on_the_scopes_bus(monkeypatch):
    monkeypatch.setattr(focus, "_logs", {})
    monkeypatch.setattr(
        focus.Config, "seestars", [{"name": "Focus Test Scope", "device_num": 7}]
    )
    focus.listen()
    focus.listen()  # Installing twice must not record twice.
    bus = blinker.signal("Focus Test Scope.eventbus")
    bus.send(_event("start"))
    bus.send(_event("working", result={"points": [[1310, 18.9]]}))
    assert focus.focus_log(7).current()["points"] == [(1310, 18.9)]
    assert len(bus.receivers) == 1
    bus.receivers.clear()


class _FakeAutomation:
    def status(self):
        return {}

    def start(self):
        pass


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(roof_web, "get_automation", lambda: _FakeAutomation())
    monkeypatch.setattr(dashboard, "get_automation", lambda: _FakeAutomation())
    monkeypatch.setattr(focus, "_logs", {})
    app = falcon.App()
    roof_web.install(app)
    return falcon.testing.TestClient(app)


def test_fragment_is_empty_with_no_run_so_the_image_stays(client):
    reply = client.simulate_get("/1/dashboard/focus")
    assert reply.status == falcon.HTTP_200
    assert reply.text.strip() == ""


def test_fragment_draws_the_curve(client):
    _finish(focus.focus_log(1))
    text = client.simulate_get("/1/dashboard/focus").text
    assert 'class="dash-focus"' in text and "<svg" in text
    assert "Settled at step 1176" in text
    assert "Focus settled at step 1176" in text


def test_fragment_escapes_the_scopes_error(client):
    log = focus.focus_log(1)
    log.on_event(_event("start"))
    log.on_event(_event("fail", error="<b>bad</b>"))
    text = client.simulate_get("/1/dashboard/focus").text
    assert "<b>bad</b>" not in text and "&lt;b&gt;bad" in text


def test_fragment_never_asks_the_scope(client, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("the focus fragment must not message the scope")

    monkeypatch.setattr(front_app, "method_sync", fail)
    monkeypatch.setattr(front_app, "do_action_device", fail)
    _finish(focus.focus_log(1))
    assert client.simulate_get("/1/dashboard/focus").status == falcon.HTTP_200


def test_dashboard_page_polls_for_the_curve_inside_the_image_panel(client, monkeypatch):
    monkeypatch.setattr(
        front_app, "get_context", lambda *a: {"root": "/1", "telescope": {}, "telescopes": []}
    )
    text = client.simulate_get("/1/dashboard").text
    assert 'hx-get="/1/dashboard/focus"' in text
    slot = text.index("dash-image-slot\"")
    assert slot < text.index('id="dash-focus"') < text.index("dash-image-view\"")
