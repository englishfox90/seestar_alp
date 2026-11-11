"""Roof history and morning summary: the record, the hook, and the read-only routes."""

import json
import logging
import os
import socket
import threading
from types import SimpleNamespace

import falcon
import falcon.testing
import pytest
import requests

from roof import automation, dashboard, night_web
from roof import web as roof_web
from roof.config import Settings
from roof.night_history import (
    KEEP_S,
    MAX_ROWS,
    SAFE,
    UNKNOWN,
    UNSAFE,
    NightHistory,
    night_window,
    reading_state,
)
from roof.queue import Target
from roof.sentinel import SafetyReading
from roof.sessions import SessionLog
from roof.sun import night_key
from tests.test_roof import NIGHT_START, Rig

LON = -99.37
NIGHT = night_key(NIGHT_START, LON)
START, END = night_window(NIGHT, LON)
OK = SafetyReading(True, True, True, "safe")
BAD = SafetyReading(False, True, False, "IsSafe is not true")
FAILED = SafetyReading(False, None, None, "read failed: timed out")
DISCONNECTED = SafetyReading(False, False, None, "not connected; reconnecting")


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "history.json")


@pytest.fixture
def history(path):
    return NightHistory(path)


def _mark(history, reading, at):
    history.record(reading, night_key(at, LON), at)


@pytest.mark.parametrize(
    "reading, state",
    [(OK, SAFE), (BAD, UNSAFE), (FAILED, UNKNOWN), (DISCONNECTED, UNKNOWN)],
)
def test_reading_state_tells_a_failed_read_from_a_real_unsafe(reading, state):
    assert reading_state(reading) == state


def test_night_window_is_the_inverse_of_night_key():
    assert night_key(START, LON) == NIGHT
    assert night_key(END - 1, LON) == NIGHT
    assert night_key(END, LON) != NIGHT
    assert END - START == 86400


# ---- closed periods ---------------------------------------------------------


def test_no_readings_means_no_periods_and_no_start(history):
    assert history.closed_periods(NIGHT, LON, NIGHT_START) == []
    assert history.recording_since() is None


def test_periods_have_start_and_end_and_a_running_one_is_open(history):
    _mark(history, OK, START + 100)
    _mark(history, BAD, START + 200)
    _mark(history, OK, START + 300)
    _mark(history, BAD, START + 400)
    assert history.closed_periods(NIGHT, LON, START + 500) == [
        {"start": START + 200, "end": START + 300, "open": False, "reason": UNSAFE},
        {"start": START + 400, "end": None, "open": True, "reason": UNSAFE},
    ]
    assert history.recording_since() == START + 100


def test_a_failed_read_counts_as_closed_with_its_own_reason(history):
    _mark(history, OK, START + 100)
    _mark(history, FAILED, START + 200)
    _mark(history, OK, START + 300)
    assert history.closed_periods(NIGHT, LON, START + 400) == [
        {"start": START + 200, "end": START + 300, "open": False, "reason": UNKNOWN}
    ]


def test_a_real_unsafe_reading_decides_a_mixed_period_either_way(history):
    _mark(history, FAILED, START + 100)
    _mark(history, BAD, START + 200)
    _mark(history, FAILED, START + 300)
    assert history.closed_periods(NIGHT, LON, START + 400) == [
        {"start": START + 100, "end": None, "open": True, "reason": UNSAFE}
    ]


def test_a_closure_is_clipped_to_each_night_it_spans(history):
    _mark(history, BAD, START + 3600)
    _mark(history, OK, END + 3600)
    next_night = night_key(END + 1, LON)
    now = END + 7200
    assert history.closed_periods(NIGHT, LON, now) == [
        {"start": START + 3600, "end": END, "open": False, "reason": UNSAFE}
    ]
    assert history.closed_periods(next_night, LON, now) == [
        {"start": END, "end": END + 3600, "open": False, "reason": UNSAFE}
    ]


def test_a_closure_from_an_earlier_night_is_still_open_tonight(history):
    _mark(history, BAD, START - 3600)
    assert history.closed_periods(NIGHT, LON, START + 100) == [
        {"start": START, "end": None, "open": True, "reason": UNSAFE}
    ]


def test_a_closure_never_reopened_is_not_open_on_a_past_night(history):
    _mark(history, BAD, START + 3600)
    assert history.closed_periods(NIGHT, LON, END + 100) == [
        {"start": START + 3600, "end": END, "open": False, "reason": UNSAFE}
    ]


def test_other_nights_periods_are_left_out(history):
    _mark(history, BAD, START - 7200)
    _mark(history, OK, START - 3600)
    _mark(history, BAD, END + 3600)
    assert history.closed_periods(NIGHT, LON, END + 7200) == []


def test_a_night_that_has_not_begun_has_no_periods(history):
    _mark(history, BAD, START - 100)
    assert history.closed_periods(NIGHT, LON, START - 50) == []


# ---- persistence ------------------------------------------------------------


def test_only_transitions_are_stored(history, path):
    for offset, reading in enumerate([OK, OK, BAD, BAD, OK]):
        _mark(history, reading, START + offset)
    rows = json.load(open(path))
    assert [(r["at"], r["state"]) for r in rows] == [
        (START, SAFE),
        (START + 2, UNSAFE),
        (START + 4, SAFE),
    ]
    assert {r["night"] for r in rows} == {NIGHT}


def test_a_restart_continues_the_period(history, path):
    _mark(history, BAD, START + 100)
    reopened = NightHistory(path)
    _mark(reopened, BAD, START + 200)  # the loop's first reading after a restart
    _mark(reopened, OK, START + 300)
    assert reopened.closed_periods(NIGHT, LON, START + 400) == [
        {"start": START + 100, "end": START + 300, "open": False, "reason": UNSAFE}
    ]


def test_old_nights_are_pruned_but_the_state_at_the_cutoff_is_kept(history, path):
    _mark(history, BAD, START - KEEP_S - 3000)
    _mark(history, OK, START - KEEP_S - 2000)
    _mark(history, BAD, START - KEEP_S - 1000)
    _mark(history, OK, START + 100)
    rows = json.load(open(path))
    assert [r["at"] for r in rows] == [START - KEEP_S - 1000, START + 100]


def test_a_flapping_monitor_cannot_grow_the_file_without_limit(history, path):
    for i in range(MAX_ROWS + 50):
        _mark(history, OK if i % 2 else BAD, START + i)
    assert len(json.load(open(path))) == MAX_ROWS


def test_a_damaged_file_is_kept_aside(path):
    with open(path, "w") as f:
        f.write("{not json")
    assert NightHistory(path).recording_since() is None
    assert os.path.exists(f"{path}.damaged")


def test_malformed_rows_are_ignored(path):
    with open(path, "w") as f:
        json.dump(["x", {"at": "soon", "state": SAFE}, {"at": 5, "state": "?"}, {"at": 9, "state": SAFE}], f)
    assert NightHistory(path).recording_since() == 9


def test_a_failed_write_leaves_the_old_file_and_goes_out_with_the_next_call(
    history, path, monkeypatch
):
    _mark(history, OK, START + 100)
    before = open(path).read()
    real_replace = os.replace

    def broken(*args):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError):
        _mark(history, BAD, START + 200)
    assert open(path).read() == before
    assert history.closed_periods(NIGHT, LON, START + 300)[0]["start"] == START + 200

    monkeypatch.setattr(os, "replace", real_replace)
    _mark(history, BAD, START + 300)  # no change, but it still owes a write
    assert [r["at"] for r in json.load(open(path))] == [START + 100, START + 200]


def test_recording_is_thread_safe(history, path):
    errors = []

    def writer(offset):
        try:
            for i in range(50):
                _mark(history, OK if i % 2 else BAD, START + offset + i)
                history.closed_periods(NIGHT, LON, END)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i * 100,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert json.load(open(path))  # a whole file, not a partial write
    assert not os.path.exists(f"{path}.tmp")


# ---- the automation hook ----------------------------------------------------


class _Raising:
    def __init__(self):
        self.calls = 0

    def record(self, *args):
        self.calls += 1
        raise RuntimeError("history is broken")


def _rig(tmp_path, name, recorder=None):
    folder = tmp_path / name
    folder.mkdir()
    rig = Rig(folder)
    rig.night_history = recorder
    rig.auto = rig.build()
    return rig


def _run_a_night(rig):
    """A start, a roof closing, a failed read, parking, and a reopening."""
    rig.settle_and_start()
    rig.safety = BAD
    rig.tick(10)
    rig.tick(40)
    rig.safety = FAILED
    rig.tick(10)
    rig.set_scope(parked=True, sched_state="stopped")
    rig.tick(60)
    rig.safety = OK
    rig.tick(10)
    rig.tick(121)


def _observable(rig):
    status = rig.auto.status()
    log = [line[21:] for line in status.pop("log")]  # past the wall-clock time
    return {
        "calls": rig.scope.calls,
        "status": status,
        "log": log,
        "phase": rig.auto.phase,
        "alarm": rig.auto._alarm,
        "was_safe": rig.auto._was_safe,
        "safe_since": rig.auto._safe_since,
        "unsafe_since": rig.auto._unsafe_since,
        "saved_state": open(rig.state_path).read() if os.path.exists(rig.state_path) else None,
    }


def test_a_raising_recorder_leaves_the_tick_unchanged(tmp_path, caplog):
    plain = _rig(tmp_path, "plain")
    recorder = _Raising()
    broken = _rig(tmp_path, "broken", recorder)
    _run_a_night(plain)
    with caplog.at_level(logging.WARNING, logger="roof"):
        _run_a_night(broken)
    assert recorder.calls >= 3
    assert _observable(broken) == _observable(plain)
    assert "park" in broken.scope.calls  # the night did reach the safety actions
    assert "could not record the roof history" in caplog.text


def test_recorder_defaults_to_none_and_the_loop_runs_without_it(rig_plain):
    assert rig_plain.auto._night_history is None
    rig_plain.tick()
    assert rig_plain.auto._was_safe is True


@pytest.fixture
def rig_plain(tmp_path):
    return Rig(tmp_path)


def test_recorder_sees_each_change_once(tmp_path):
    calls = []
    recorder = SimpleNamespace(record=lambda *args: calls.append(args))
    rig = _rig(tmp_path, "rig", recorder)
    rig.tick()
    rig.tick(10)
    rig.safety = BAD
    rig.tick(10)
    rig.tick(10)
    rig.safety = OK
    rig.tick(10)
    night = night_key(rig.now, rig.settings.lon)
    assert calls == [
        (OK, night, NIGHT_START),
        (BAD, night, NIGHT_START + 20),
        (OK, night, NIGHT_START + 40),
    ]


def test_a_real_history_through_the_loop(tmp_path, path):
    rig = _rig(tmp_path, "rig", NightHistory(path))
    rig.tick()
    rig.safety = FAILED
    rig.tick(10)
    rig.safety = OK
    rig.tick(10)
    assert rig.auto._night_history.closed_periods(NIGHT, LON, rig.now) == [
        {
            "start": NIGHT_START + 10,
            "end": NIGHT_START + 20,
            "open": False,
            "reason": UNKNOWN,
        }
    ]


def test_the_process_wide_automation_stores_it_beside_the_session_log(tmp_path, monkeypatch):
    from device.config import Config

    # A handler present already, so get_automation adds no log file of its own.
    logger = logging.getLogger("roof")
    handler = logging.NullHandler()
    logger.addHandler(handler)
    monkeypatch.setattr(automation, "_instance", None)
    monkeypatch.setattr(automation, "_target_queue", None, raising=False)
    monkeypatch.setattr(Config, "path_to_dat", str(tmp_path / "config.toml"))
    try:
        history = automation.night_history()
    finally:
        logger.removeHandler(handler)
    assert history._path == str(tmp_path / "roof_night_history.json")


# ---- the morning summary ----------------------------------------------------


def _log(tmp_path, rows):
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps(rows))
    return SessionLog(str(path))


def _row(name, stacked, dropped=0, exposure=30.0, night=NIGHT, started=1000.0, wall=7200.0):
    return {
        "target": " ".join(name.lower().split()).split("_")[0],
        "name": name,
        "night": night,
        "started": started,
        "exposure_s": exposure,
        "stacked": stacked,
        "dropped": dropped,
        "wall_s": wall,
    }


def test_summary_is_empty_when_there_is_nothing(tmp_path):
    assert night_web.night_summary(NIGHT, None, []) == {}
    assert night_web.night_summary(NIGHT, _log(tmp_path, []), []) == {}
    log = _log(tmp_path, [_row("M 42", 10, night="2020-01-01")])
    assert night_web.night_summary(NIGHT, log, []) == {}


def test_summary_adds_a_targets_runs_and_keeps_only_that_night(tmp_path):
    target = Target(id="a", name="NGC 6992", ra=1, dec=2, goal_hours=10, prior_hours=1)
    log = _log(
        tmp_path,
        [
            _row("NGC 6992", 120, dropped=30, night="2026-11-01", started=1.0),
            _row("NGC 6992", 120, dropped=20, started=1000.0),
            _row("NGC 6992", 240, dropped=20, started=9000.0),
        ],
    )
    summary = night_web.night_summary(NIGHT, log, [target])
    assert summary == {
        "night": NIGHT,
        "targets": [
            {
                "name": "NGC 6992",
                "hours": 3.0,  # 360 frames at 30 s
                "hours_not_counted": 0.0,
                "stacked": 360,
                "dropped": 40,
                "kept_percent": 90,
                "goal": {"have_hours": 5.0, "goal_hours": 10, "text": "5.0 of 10 h"},
            }
        ],
        "total_hours": 3.0,
        "total_stacked": 360,
        "total_dropped": 40,
        "kept_percent": 90,
    }
    assert summary["targets"][0]["goal"]["text"] == dashboard.goal_text(target, log)


def test_summary_totals_targets_and_has_no_goal_for_one_off_the_list(tmp_path):
    log = _log(
        tmp_path,
        [
            _row("Off List_123", 120, exposure=20.0, started=2000.0),
            _row("M 42", 60, started=1000.0),
        ],
    )
    target = Target(id="a", name="M 42", ra=1, dec=2)
    summary = night_web.night_summary(NIGHT, log, [target])
    assert [t["name"] for t in summary["targets"]] == ["M 42", "Off List"]
    assert summary["targets"][1]["goal"] is None
    assert summary["targets"][1]["hours"] == round(120 * 20 / 3600, 2)
    assert summary["total_hours"] == round(0.5 + 120 * 20 / 3600, 2)
    assert summary["kept_percent"] == 100


def test_summary_marks_a_framed_run_with_holes_as_not_counted_toward_the_goal(tmp_path):
    framed = Target(
        id="a", name="IC 1805", ra=1, dec=2, goal_hours=8, mosaic_scale=1.7, min_session_hours=4
    )
    log = _log(tmp_path, [{**_row("IC 1805", 120, wall=3600.0), "covered": False}])
    entry = night_web.night_summary(NIGHT, log, [framed])["targets"][0]
    assert entry["hours"] == 1.0
    assert entry["hours_not_counted"] == 1.0
    assert entry["goal"]["have_hours"] == 0.0
    assert entry["goal"]["text"] == "0.0 of 8 h, plus 1.0 h not counted, framing not covered"


def test_summary_invents_no_hours_for_a_run_with_no_exposure_known(tmp_path):
    log = _log(tmp_path, [_row("M 42", 60, exposure=0)])
    summary = night_web.night_summary(NIGHT, log, [])
    assert summary["total_hours"] == 0
    assert summary["total_stacked"] == 60


# ---- routes -----------------------------------------------------------------


class _FakeAutomation:
    def status(self):
        return {}

    def start(self):
        pass


def _refuse(*args, **kwargs):
    raise AssertionError("this route must not reach anything outside the process")


@pytest.fixture
def client(monkeypatch, tmp_path, history):
    log = _log(tmp_path, [_row("M 42", 120, dropped=40)])
    target = Target(id="a", name="M 42", ra=1, dec=2, goal_hours=10)
    settings = Settings(lon=LON, lat=31.5)
    monkeypatch.setattr(roof_web, "get_automation", lambda: _FakeAutomation())
    monkeypatch.setattr(dashboard, "_stores", {})
    monkeypatch.setattr(night_web.roof_config, "load", lambda: settings)
    monkeypatch.setattr(night_web, "night_history", lambda: history)
    monkeypatch.setattr(night_web, "session_log", lambda: log)
    monkeypatch.setattr(night_web, "target_queue", lambda: SimpleNamespace(all=lambda: [target]))
    monkeypatch.setattr(night_web.time, "time", lambda: START + 500)
    app = falcon.App()
    roof_web.install(app)
    return falcon.testing.TestClient(app)


def _no_way_out(monkeypatch):
    for method in ("get", "put", "post", "request"):
        monkeypatch.setattr(requests, method, _refuse)
    monkeypatch.setattr(requests.Session, "request", _refuse)
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(dashboard, "_imager", _refuse)
    monkeypatch.setattr(dashboard.ImageStore, "_maybe_request", _refuse)
    monkeypatch.setattr(dashboard, "_front", _refuse)
    monkeypatch.setattr(automation.sentinel, "read", _refuse)


def test_night_json_serves_tonight_and_only_reads(client, history, monkeypatch):
    _mark(history, BAD, START + 100)
    _mark(history, OK, START + 200)
    _mark(history, FAILED, START + 300)
    _no_way_out(monkeypatch)
    response = client.simulate_get("/1/dashboard/night.json")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    assert response.json["night"] == NIGHT
    assert response.json["now"] == START + 500
    assert response.json["roof"] == {
        "recording_since": START + 100,
        "closed": [
            {"start": START + 100, "end": START + 200, "open": False, "reason": UNSAFE},
            {"start": START + 300, "end": None, "open": True, "reason": UNKNOWN},
        ],
    }
    assert response.json["summary"]["total_hours"] == 1.0
    assert response.json["summary"]["kept_percent"] == 75
    assert response.json["summary"]["targets"][0]["goal"]["text"] == "1.0 of 10 h"


def test_night_json_with_nothing_recorded(client, monkeypatch):
    monkeypatch.setattr(night_web, "night_history", lambda: None)
    monkeypatch.setattr(night_web, "session_log", lambda: None)
    assert client.simulate_get("/1/dashboard/night.json").json == {
        "night": NIGHT,
        "now": START + 500,
        "roof": {"recording_since": None, "closed": []},
        "summary": {},
    }


def test_a_bad_target_list_does_not_lose_the_roof_periods(client, history, monkeypatch):
    _mark(history, BAD, START + 100)

    def unreadable():
        raise RuntimeError("target list is unreadable")

    monkeypatch.setattr(night_web, "target_queue", unreadable)
    body = client.simulate_get("/1/dashboard/night.json").json
    assert body["roof"]["closed"][0]["start"] == START + 100
    assert body["summary"] == {}
    assert "unreadable" in body["summary_error"]


def _hold_image(monkeypatch, image):
    imager = object()
    store = SimpleNamespace(latest=lambda seen, fetch=False: _latest(seen, fetch, imager, image))
    monkeypatch.setattr(dashboard, "_imager", lambda telescope_id: imager)
    monkeypatch.setattr(dashboard, "_store", lambda telescope_id: store)


def _latest(seen, fetch, imager, image):
    assert seen is imager
    assert not fetch  # a viewer must not prompt a download from the scope
    return image


def test_image_view_is_the_full_image_shown_inline(client, monkeypatch):
    _hold_image(monkeypatch, {"full": b"full-size", "preview": b"small"})
    response = client.simulate_get("/1/dashboard/image-view.jpg")
    assert response.status_code == 200
    assert response.content == b"full-size"
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "no-store"
    assert "content-disposition" not in response.headers


def test_image_view_without_an_image(client, monkeypatch):
    _hold_image(monkeypatch, None)
    assert client.simulate_get("/1/dashboard/image-view.jpg").status_code == 404


def test_download_route_still_sends_an_attachment(client, monkeypatch):
    image = {"full": b"x", "preview": b"y", "target": "M 42", "stacked": 3, "captured_at": 0}
    _hold_image(monkeypatch, image)
    response = client.simulate_get("/1/dashboard/image.jpg", query_string="full=true")
    assert "attachment" in response.headers["content-disposition"]
