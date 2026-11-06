"""Target list, session record and planner."""

import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import ephem
import pytest

from roof import planner, schedule_tools
from roof.planner import PlanConfig, moon_required_separation, plan_night, to_schedule
from roof.queue import (
    FramingEstimates,
    Target,
    TargetQueue,
    apply_estimates,
    targets_from_plan,
    targets_from_schedule,
)
from roof.scope import RealScope, ScopeState
from roof.automation import IDLE
from roof.sentinel import SafetyReading
from roof.sessions import SessionLog
from roof.sun import night_key

SAFE = SafetyReading(True, True, True, "safe")
UNSAFE = SafetyReading(False, True, False, "IsSafe is not true")
CFG = PlanConfig(lat=31.501, lon=-99.3734)
NIGHT = "2026-11-05"  # moon about 11%, dark about 19:10 to 05:30 CST


def single(name="NGC 1499", ra=4.0, dec=36.4, **kw):
    return Target(id=name, name=name, ra=ra, dec=dec, **kw)


def framed(name="NGC 6960", ra=20.87, dec=30.48, **kw):
    kw.setdefault("min_session_hours", 4.0)
    return Target(id=name, name=name, ra=ra, dec=dec, mosaic_scale=1.7, mosaic_angle=10.0, **kw)


def remaining(*targets):
    return {t.id: t.goal_hours for t in targets}


def stacking(target="NGC 6960", stacked=100, lapse=3600.0, exposure=20.0, dropped=0):
    return ScopeState(
        view_state="working",
        view_stage="Stack",
        view_target=target,
        stacked=stacked,
        dropped=dropped,
        exposure_s=exposure,
        stack_lapse_s=lapse,
    )


# ---- moon ------------------------------------------------------------------


def test_moon_separation_is_full_at_full_moon_and_small_at_new():
    assert moon_required_separation(14.75, 120, 14) == pytest.approx(120)
    assert moon_required_separation(1.0, 120, 14) < 70


def test_target_beside_a_full_moon_is_refused_with_the_reason():
    key = "2026-11-24"
    observer = ephem.Moon(datetime(2026, 11, 25, 6, tzinfo=timezone.utc).replace(tzinfo=None))
    beside = single(
        "Beside moon",
        ra=float(observer.a_ra) * 12 / ephem.pi,
        dec=float(observer.a_dec) * 180 / ephem.pi,
    )
    plan = plan_night([beside], remaining(beside), key, CFG)
    assert plan.moon_percent > 90
    assert not plan.blocks
    assert plan.skipped[beside.id]  # moon or a window it cut short


# ---- planning --------------------------------------------------------------


def test_plan_stays_in_the_dark_and_above_the_limit():
    target = single()
    plan = plan_night([target], remaining(target), NIGHT, CFG)
    assert plan.blocks
    assert plan.blocks[0].start >= plan.dark_start
    assert plan.blocks[-1].end <= plan.dark_end
    altitude = ephem.Observer()
    altitude.lat, altitude.lon, altitude.pressure = str(CFG.lat), str(CFG.lon), 0
    body = ephem.FixedBody()
    body._ra, body._dec, body._epoch = ephem.hours("4:00"), ephem.degrees("36.4"), ephem.J2000
    altitude.date = ephem.Date(datetime.fromtimestamp(plan.blocks[0].start, timezone.utc).replace(tzinfo=None))
    body.compute(altitude)
    assert body.alt * 180 / ephem.pi >= CFG.min_alt_deg


def test_framed_target_gets_its_whole_minimum_in_one_run():
    target = framed()
    plan = plan_night([target], remaining(target), NIGHT, CFG)
    assert len(plan.blocks) == 1
    assert plan.blocks[0].stack_s >= 4 * 3600


def test_framed_target_is_skipped_when_no_window_is_long_enough():
    target = framed(min_session_hours=12.0)
    plan = plan_night([target], remaining(target), NIGHT, CFG)
    assert not plan.blocks
    assert "12 h framed run" in plan.skipped[target.id]


def test_skip_reason_is_about_what_is_left_of_the_night():
    target = framed()
    whole = plan_night([target], remaining(target), NIGHT, CFG)
    late = whole.dark_end - 3 * 3600
    plan = plan_night([target], remaining(target), NIGHT, CFG, start=late)
    assert "scored higher" not in plan.skipped[target.id]


def test_framed_run_finishes_even_when_less_than_its_minimum_is_left():
    target = framed(goal_hours=1.0)
    plan = plan_night([target], remaining(target), NIGHT, CFG)
    assert plan.blocks[0].stack_s >= 4 * 3600


def test_singles_cannot_eat_a_framed_targets_only_window():
    # NGC 6960 sets early, so its window shrinks with every hour spent elsewhere.
    wide = framed()
    others = [single(f"Single {n}", ra=4.0 + n / 10, dec=36.0, priority="high") for n in range(3)]
    targets = others + [wide]
    plan = plan_night(targets, remaining(*targets), NIGHT, CFG)
    runs = [b for b in plan.blocks if b.target is wide]
    assert runs and runs[0].stack_s >= 4 * 3600


def test_finished_and_disabled_targets_are_left_out():
    done, off = single("Done"), single("Off", enabled=False)
    rem = {done.id: 0.0, off.id: 5.0}
    plan = plan_night([done, off], rem, NIGHT, CFG)
    assert not plan.blocks
    assert plan.skipped == {done.id: "goal reached", off.id: "disabled"}


def test_stops_when_the_goal_is_met_and_moves_on():
    first = single("First", goal_hours=1.0, priority="high", min_session_hours=1.0)
    second = single("Second", ra=4.2, goal_hours=20.0)
    plan = plan_night([first, second], remaining(first, second), NIGHT, CFG)
    assert [b.target.name for b in plan.blocks][:2] == ["First", "Second"]


def test_planning_spends_down_remaining_hours():
    target = single(goal_hours=2.0)
    rem = remaining(target)
    plan_night([target], rem, NIGHT, CFG)
    assert rem[target.id] <= 0


def test_starting_late_plans_only_what_is_left():
    target = single()
    whole = plan_night([target], remaining(target), NIGHT, CFG)
    late = whole.blocks[0].start + 2 * 3600
    plan = plan_night([target], remaining(target), NIGHT, CFG, start=late)
    assert plan.blocks[0].start >= late


def test_measured_rate_replaces_the_assumed_one():
    target = single()
    assumed = plan_night([target], remaining(target), NIGHT, CFG)
    measured = plan_night([target], remaining(target), NIGHT, CFG, rates={target.id: 0.25})
    assert measured.blocks[0].expected_hours < assumed.blocks[0].expected_hours


def test_forecast_carries_progress_between_nights(tmp_path):
    target = single(goal_hours=12.0)
    log = SessionLog(str(tmp_path / "s.json"))
    now = datetime(2026, 11, 5, 18, tzinfo=timezone.utc).timestamp()
    plans = planner.forecast([target], log, CFG, now, nights=5)
    finished = planner.finish_nights(plans, [target], log)
    assert target.id in finished
    assert finished[target.id] > plans[0].key
    assert not plans[-1].blocks


# ---- schedules -------------------------------------------------------------


def test_plan_converts_to_a_schedule_the_automation_accepts():
    first, second = framed(exposure_s=20.0), single(exposure_s=30.0)
    plan = plan_night([first, second], remaining(first, second), NIGHT, CFG)
    doc = to_schedule(plan)
    errors, _ = schedule_tools.validate_items(doc["list"])
    assert not errors
    actions = [i["action"] for i in doc["list"]]
    assert "start_framed_mosaic" in actions and "start_mosaic" in actions
    item = next(i for i in doc["list"] if i["action"] == "start_framed_mosaic")
    assert item["params"]["mosaic_scale"] == 1.7
    assert item["params"]["panel_time_sec"] == plan.blocks[0].stack_s


# ---- importing -------------------------------------------------------------

PLAN = {
    "plan_name": "Cygnus",
    "list": [
        {
            "target_name": "NGC 6960",
            "target_ra_dec": [20.872814, 30.481741],
            "lp_filter": True,
            "mosaic": {"scale": 1.7, "angle": 190.0, "star_map_angle": 12.05},
        },
        {"target_name": "M 42", "target_ra_dec": [5.5881, -5.39], "lp_filter": False},
        {"target_name": "here", "target_ra_dec": [-1.0, -1.0]},
    ],
}


def test_app_plan_keeps_its_framing_and_asks_for_a_long_run():
    found = targets_from_plan(PLAN)
    assert [t["name"] for t in found] == ["NGC 6960", "M 42"]
    assert found[0]["mosaic_scale"] == 1.7
    assert found[0]["mosaic_angle"] == 10.0
    assert found[0]["mosaic_star_map_angle"] == 12.05
    assert found[0]["min_session_hours"] == 4.0
    assert "mosaic_scale" not in found[1]


def test_stored_schedule_imports_with_its_exposure():
    found = targets_from_schedule(
        schedule_tools.plan_to_schedule(PLAN)["list"]
        + [{"action": "start_mosaic", "params": {"target_name": "x", "ra": -1, "dec": -1}}],
        "Cygnus.json",
    )
    assert [t["name"] for t in found] == ["NGC 6960", "M 42"]
    assert found[0]["exposure_s"] == 30.0
    assert found[0]["mosaic_scale"] == 1.7


def test_reimport_updates_framing_but_keeps_goal_and_settings(tmp_path):
    queue = TargetQueue(str(tmp_path / "t.json"))
    assert queue.merge(targets_from_plan(PLAN), goal_hours=12.0) == (2, 0)
    first = queue.all()[0]
    queue.update(first.id, goal_hours=30.0, priority="high")
    changed = json.loads(json.dumps(PLAN))
    changed["list"][0]["mosaic"]["angle"] = 45.0
    changed["list"][0]["target_name"] = "ngc  6960"
    assert queue.merge(targets_from_plan(changed), goal_hours=5.0) == (0, 2)
    again = queue.all()[0]
    assert again.goal_hours == 30.0 and again.priority == "high"
    assert again.mosaic_angle == 45.0 and again.name == "NGC 6960"


def test_unreadable_target_list_is_not_overwritten(tmp_path):
    path = tmp_path / "t.json"
    path.write_text("{broken")
    queue = TargetQueue(str(path))
    with pytest.raises(RuntimeError):
        queue.merge(targets_from_plan(PLAN))
    assert path.read_text() == "{broken"


class _Device:
    def __init__(self, **replies):
        self.replies, self.asked = replies, []

    def send_message_param_sync(self, data):
        self.asked.append(data["method"])
        return self.replies.get(data["method"], {"error": "method not found", "code": 103})


def test_loaded_plan_reads_app_state():
    device = _Device(iscope_get_app_state={"result": {"ViewPlan": {"plan": PLAN}}})
    assert schedule_tools.loaded_plan(device) is PLAN


def test_loaded_plan_is_none_when_the_scope_holds_none():
    device = _Device(iscope_get_app_state={"result": {"View": {}}})
    assert schedule_tools.loaded_plan(device) is None
    assert device.asked == ["iscope_get_app_state", "get_view_plan"]


def test_loaded_plan_falls_back_for_other_firmware():
    device = _Device(iscope_get_app_state={"result": {}}, get_view_plan={"result": PLAN})
    assert schedule_tools.loaded_plan(device) is PLAN


# ---- session record --------------------------------------------------------


def test_a_run_is_one_row_however_often_it_is_seen(tmp_path):
    log = SessionLog(str(tmp_path / "s.json"))
    log.observe(stacking(stacked=10, lapse=600), "n", 1000)
    log.observe(stacking(stacked=20, lapse=1200), "n", 1600)
    rows = log.rows()
    assert len(rows) == 1 and rows[0]["stacked"] == 20


def test_a_reading_between_exposures_keeps_the_hours(tmp_path):
    log = SessionLog(str(tmp_path / "s.json"))
    log.observe(stacking("M 42", stacked=60, lapse=600, exposure=10), "n", 1000)
    log.observe(stacking("M 42", stacked=61, lapse=610, exposure=0.0), "n", 1010)
    assert log.progress(single("M 42")).counted_hours == pytest.approx(61 * 10 / 3600)


def test_a_restart_of_alp_continues_the_run(tmp_path):
    path = str(tmp_path / "s.json")
    first = SessionLog(path)
    first.observe(stacking(stacked=10, lapse=600), "n", 1000)
    first.flush()
    second = SessionLog(path)
    second.observe(stacking(stacked=30, lapse=1800), "n", 2200)
    rows = second.rows()
    assert len(rows) == 1 and rows[0]["stacked"] == 30


def test_a_new_stack_of_the_same_target_is_a_new_run(tmp_path):
    log = SessionLog(str(tmp_path / "s.json"))
    log.observe(stacking(stacked=100, lapse=3600), "n", 10000)
    log.observe(stacking(stacked=5, lapse=100), "n", 20000)
    assert len(log.rows()) == 2


def test_only_real_stacking_is_recorded(tmp_path):
    log = SessionLog(str(tmp_path / "s.json"))
    log.observe(ScopeState(view_state="working", view_stage="AutoGoto", view_target="M 42"), "n", 1)
    log.observe(stacking(stacked=0), "n", 2)
    assert log.rows() == []


def test_progress_saved_within_a_minute_and_when_stacking_ends(tmp_path):
    path = tmp_path / "s.json"
    log = SessionLog(str(path))
    log.observe(stacking(stacked=1), "n", 1000)
    assert not path.exists()
    log.observe(stacking(stacked=2), "n", 1061)
    assert json.loads(path.read_text())[0]["stacked"] == 2
    log.observe(stacking(stacked=3), "n", 1070)
    log.observe(ScopeState(), "n", 1080)
    assert json.loads(path.read_text())[0]["stacked"] == 3


def test_short_framed_run_is_shown_but_not_counted(tmp_path):
    log = SessionLog(str(tmp_path / "s.json"))
    target = framed()
    log.observe(stacking(stacked=180, lapse=3600, exposure=20), "n", 10000)
    short = log.progress(target)
    assert short.counted_hours == 0 and short.partial_hours == pytest.approx(1.0)
    log.observe(stacking(stacked=720, lapse=4 * 3600, exposure=20), "n", 90000)
    full = log.progress(target)
    assert full.counted_hours == pytest.approx(4.0)
    assert full.sessions == 2


def test_short_single_frame_run_still_counts(tmp_path):
    log = SessionLog(str(tmp_path / "s.json"))
    log.observe(stacking("M 42", stacked=60, lapse=600, exposure=10), "n", 1000)
    assert log.progress(single("M 42")).counted_hours == pytest.approx(60 * 10 / 3600)


def test_mosaic_panel_suffix_counts_for_its_target(tmp_path):
    log = SessionLog(str(tmp_path / "s.json"))
    log.observe(stacking("M 42_11", stacked=60, lapse=600, exposure=10), "n", 1000)
    assert log.progress(single("M 42")).sessions == 1


def test_damaged_record_is_kept_aside(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("[{broken")
    log = SessionLog(str(path))
    assert log.rows() == []
    assert (tmp_path / "s.json.damaged").read_text() == "[{broken"


def test_scope_read_carries_the_stack_counters():
    app_state = {
        "View": {
            "state": "working",
            "stage": "Stack",
            "target_name": "NGC 6960",
            "Stack": {
                "lapse_ms": 8584319,
                "stacked_frame": 253,
                "dropped_frame": 92,
                "Exposure": {"exp_ms": 20000.0},
            },
        }
    }
    device = SimpleNamespace(
        is_connected=True,
        device_name="scope",
        schedule={"state": "working", "list": [], "item_number": 0},
        scheduler_thread=None,
        send_message_param_sync=lambda data: {
            "result": app_state if data["method"] == "iscope_get_app_state" else {}
        },
    )
    state = RealScope(lambda: device).read()
    assert (state.view_target, state.stacked, state.dropped) == ("NGC 6960", 253, 92)
    assert state.exposure_s == 20.0
    assert state.stack_lapse_s == pytest.approx(8584.319)


# ---- page ------------------------------------------------------------------


@pytest.fixture
def page(monkeypatch, tmp_path):
    import falcon
    import falcon.testing

    from roof import queue_web
    from roof import web as roof_web
    from roof.config import Settings

    class Fake:
        requests = []

        def request(self, command):
            self.requests.append(command)

        def status(self):
            return {}

    log = SessionLog(str(tmp_path / "s.json"))
    monkeypatch.setattr(roof_web, "get_automation", Fake)
    monkeypatch.setattr(queue_web, "session_log", lambda: log)
    monkeypatch.setattr(queue_web, "_base", lambda: str(tmp_path))
    monkeypatch.setattr(roof_web.focus, "listen", lambda: None)
    monkeypatch.setattr(
        queue_web.roof_config,
        "load",
        lambda: Settings(lat=31.501, lon=-99.3734, min_target_alt_deg=20.0),
    )
    monkeypatch.setattr(queue_web.planner, "config_from_settings", lambda s: CFG)
    monkeypatch.chdir(tmp_path)
    app = falcon.App()
    app.req_options.auto_parse_form_urlencoded = False
    for path, resource in queue_web.PAGES.items():
        app.add_route(f"/{path}", resource())
    return falcon.testing.TestClient(app), queue_web, tmp_path


FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def test_page_actions_edit_the_list(page, monkeypatch):
    client, queue_web, _ = page
    monkeypatch.setattr(queue_web, "_front", lambda: _NoPage(), raising=False)
    import roof.web

    monkeypatch.setattr(roof.web, "_front", lambda: _NoPage())
    body = "name=M+42&ra=5h35m17s&dec=-5d23m&goal_hours=6&lp_filter=on"
    assert client.simulate_post("/targets/add", headers=FORM, body=body).status_code == 302
    target = queue_web._queue().all()[0]
    assert target.goal_hours == 6.0 and target.lp_filter
    assert target.ra == pytest.approx(5.5881, abs=0.001)

    body = f"id={target.id}&goal_hours=9&min_session_hours=2&prior_hours=1.5&priority=high&enabled=on"
    client.simulate_post("/targets/update", headers=FORM, body=body)
    target = queue_web._queue().all()[0]
    assert (target.goal_hours, target.min_session_hours, target.prior_hours) == (9.0, 2.0, 1.5)
    assert target.priority == "high" and target.enabled

    client.simulate_post("/targets/remove", headers=FORM, body=f"id={target.id}")
    assert queue_web._queue().all() == []


@pytest.mark.parametrize(
    "body",
    [
        "name=&ra=1&dec=1",
        "name=x&ra=25&dec=1",
        "name=x&ra=1&dec=91",
        "name=x&ra=nonsense&dec=1",
        "name=x&ra=1&dec=1&goal_hours=-1",
    ],
)
def test_bad_targets_are_refused(page, monkeypatch, body):
    client, queue_web, _ = page
    import roof.web

    monkeypatch.setattr(roof.web, "_front", lambda: _NoPage())
    assert client.simulate_post("/targets/add", headers=FORM, body=body).status_code == 400
    assert queue_web._queue().all() == []


def test_cross_origin_post_to_the_list_is_refused(page):
    client, _, _ = page
    result = client.simulate_post(
        "/targets/remove",
        headers={"Origin": "http://evil.example", "Host": "localhost:5432"},
    )
    assert result.status_code == 403


def test_context_reports_progress_and_tonight(page, monkeypatch):
    _, queue_web, _ = page
    monkeypatch.setattr(
        queue_web.time, "time", lambda: datetime(2026, 11, 6, 2, tzinfo=timezone.utc).timestamp()
    )
    queue_web._queue().merge(targets_from_plan(PLAN), goal_hours=4.0)
    context = queue_web._context()
    assert {r["target"].name for r in context["rows"]} == {"NGC 6960", "M 42"}
    assert context["tonight"]["blocks"]
    assert len(context["nights"]) == queue_web.FORECAST_NIGHTS - 1


class _NoPage:
    def flash(self, resp, message):
        self.message = message

    def redirect(self, location):
        import falcon

        raise falcon.HTTPFound(location)


def test_nav_lists_the_page_under_functions():
    from pathlib import Path

    nav = (Path(__file__).parent.parent / "front/templates/nav.html").read_text()
    menu = nav[nav.index(">Functions"):]
    assert '{{ root }}/targets">Target List<' in menu[: menu.index("</ul>")]


# ---- writing the night's plan ----------------------------------------------

SETTINGS = SimpleNamespace(lat=CFG.lat, lon=CFG.lon, sun_max_deg=-18.0, min_target_alt_deg=20.0)
DUSK = datetime(2026, 11, 6, 1, 15, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def plain_config(monkeypatch):
    monkeypatch.setattr(planner, "config_from_settings", lambda s: CFG)


def test_writes_a_valid_plan_when_a_target_is_ready(tmp_path, plain_config):
    log = SessionLog(str(tmp_path / "s.json"))
    ready, why = planner.write_night_plan(SETTINGS, [framed()], log, DUSK, str(tmp_path))
    assert ready and not why
    items, errors, _ = schedule_tools.load(str(tmp_path / schedule_tools.PLAN_FILE))
    assert items and not errors


def test_not_ready_while_the_first_target_is_hours_away(tmp_path, plain_config):
    log = SessionLog(str(tmp_path / "s.json"))
    late = single("M 42", ra=5.59, dec=-5.39)
    ready, why = planner.write_night_plan(SETTINGS, [late], log, DUSK, str(tmp_path))
    assert not ready and "M 42" in why
    assert not (tmp_path / schedule_tools.PLAN_FILE).exists()


def test_not_ready_when_nothing_can_be_imaged(tmp_path, plain_config):
    log = SessionLog(str(tmp_path / "s.json"))
    ready, why = planner.write_night_plan(SETTINGS, [], log, DUSK, str(tmp_path))
    assert not ready and "rest of this night" in why


def test_schedule_ends_at_a_gap_worth_parking_for():
    early = single("A", ra=20.87, dec=30.48, goal_hours=1.0)
    late = single("B", ra=6.0, dec=-5.0, goal_hours=1.0)
    plan = plan_night([early, late], remaining(early, late), NIGHT, CFG)
    assert plan.blocks[1].start - plan.blocks[0].end >= planner.PARK_GAP_S
    names = [i["params"]["target_name"] for i in to_schedule(plan)["list"] if "target_name" in i["params"]]
    assert names == ["A"]


def test_short_gap_waits_with_the_arm_open():
    early = single("A")
    plan = planner.NightPlan(key=NIGHT, dark_start=0, dark_end=9000)
    plan.blocks = [
        planner.Block(early, 0, 3600, 180, 1.0),
        planner.Block(early, 4200, 7800, 180, 1.0),
    ]
    actions = [i["action"] for i in to_schedule(plan)["list"]]
    assert actions.count("start_mosaic") == 2 and actions.count("wait_for") == 1


# ---- automation running from the list --------------------------------------


class _ListRig:
    """The roof test rig, selecting the target list with a scripted planner."""

    def __init__(self, tmp_path):
        from tests.test_roof import Rig

        self.rig = Rig(tmp_path)
        self.rig.settings = replace(self.rig.settings, schedule=schedule_tools.TARGET_LIST)
        self.answer = (True, "")
        self.plans = 0
        self.panel_time = 3600
        self.rig.auto._plan_targets = self._plan

    def _plan(self, s, now):
        self.plans += 1
        if self.answer[0]:
            self.rig.write(
                schedule_tools.PLAN_FILE,
                [
                    {
                        "action": "start_mosaic",
                        "params": {"target_name": f"run {self.plans}", "panel_time_sec": self.panel_time},
                    }
                ],
            )
        return self.answer

    def finish_schedule(self, after):
        self.rig.set_scope(sched_state="complete")
        self.rig.tick(after)
        self.rig.set_scope(parked=True, sched_state="stopped")
        self.rig.tick(10)


@pytest.fixture
def list_rig(tmp_path):
    return _ListRig(tmp_path)


def test_waits_with_the_arm_closed_until_a_target_is_due(list_rig):
    rig = list_rig.rig
    list_rig.answer = (False, "next target is M 42 at 23:05")
    rig.tick()
    rig.tick(121)
    assert "open_arm" not in str(rig.scope.calls)
    assert rig.auto.phase == IDLE
    assert "M 42" in rig.auto.status()["note"]


def test_a_failing_planner_only_delays_the_start(list_rig):
    rig = list_rig.rig

    def broken(s, now):
        raise RuntimeError("target list unreadable")

    rig.auto._plan_targets = broken
    rig.tick()
    rig.tick(121)
    assert rig.auto.phase == IDLE
    assert "unreadable" in rig.auto.status()["note"]


def test_starts_the_plan_from_its_first_item(list_rig):
    rig = list_rig.rig
    rig.settle_and_start()
    assert ("start_scheduler", 1) in rig.scope.calls


def test_reopening_the_roof_replans_instead_of_resuming(list_rig):
    rig = list_rig.rig
    rig.settle_and_start()
    before = list_rig.plans
    rig.safety = UNSAFE
    rig.tick()
    rig.tick(31)
    rig.set_scope(parked=True, sched_state="stopped")
    rig.tick(10)
    assert rig.auto.status()["resume_item"] is None
    rig.safety = SAFE
    rig.tick()
    rig.tick(121)
    assert list_rig.plans > before


def test_a_finished_plan_parks_then_plans_again(list_rig):
    rig = list_rig.rig
    rig.settle_and_start()
    list_rig.answer = (False, "no target on the list can be imaged for the rest of this night")
    list_rig.finish_schedule(after=3600)
    assert rig.auto.status()["completed"] is False
    assert "park" in rig.scope.calls
    before = list_rig.plans
    rig.tick(10)
    assert list_rig.plans > before
    assert "rest of this night" in rig.auto.status()["note"]


def test_a_plan_ending_early_stops_for_the_night_with_an_alarm(list_rig):
    rig = list_rig.rig
    rig.settle_and_start()
    list_rig.finish_schedule(after=60)
    status = rig.auto.status()
    assert status["completed"] is True
    assert "ended well before" in status["alarm"]
    before = list_rig.plans
    rig.tick(10)
    assert list_rig.plans == before


def test_runs_a_night_are_capped(list_rig):
    from roof.automation import MAX_LIST_RUNS

    rig = list_rig.rig
    rig.settle_and_start()
    auto = rig.auto
    auto._list_runs_night = night_key(rig.now, rig.settings.lon)
    auto._list_runs = MAX_LIST_RUNS - 1
    list_rig.finish_schedule(after=3600)
    assert auto.status()["completed"] is True


def test_a_schedule_file_still_finishes_the_night(tmp_path):
    from tests.test_roof import Rig, _target

    rig = Rig(tmp_path)
    rig.write("Plan.json", [_target("A")])
    rig.settings = replace(rig.settings, schedule="Plan.json")
    rig.settle_and_start()
    rig.set_scope(sched_state="complete")
    rig.tick(3600)
    assert rig.auto.status()["completed"] is True


def test_roof_page_accepts_the_target_list_choice(monkeypatch):
    import falcon
    import falcon.testing

    from roof import web as roof_web

    saved = {}
    monkeypatch.setattr(roof_web.roof_config, "save", lambda k, v: saved.update({k: v}))
    monkeypatch.setattr(roof_web, "get_automation", lambda: SimpleNamespace(request=lambda c: None))
    monkeypatch.setattr(roof_web, "_front", lambda: _NoPage())
    app = falcon.App()
    app.req_options.auto_parse_form_urlencoded = False
    app.add_route("/roof/select", roof_web.RoofSelect())
    client = falcon.testing.TestClient(app)
    body = f"schedule={schedule_tools.TARGET_LIST}"
    assert client.simulate_post("/roof/select", headers=FORM, body=body).status_code == 302
    assert saved == {"schedule": schedule_tools.TARGET_LIST}


# ---- the scope's estimate for a framing -------------------------------------


def test_known_framing_size_needs_no_learning(tmp_path):
    estimates = FramingEstimates(str(tmp_path / "f.json"))
    assert estimates.hours(1.7) == 1.375
    assert estimates.hours(2.2) is None


def test_estimates_are_learned_and_kept(tmp_path):
    path = str(tmp_path / "f.json")
    assert FramingEstimates(path).note(2.2, 3.0) is True
    assert FramingEstimates(path).note(2.2, 3.0) is False
    assert FramingEstimates(path).hours(2.2) == 3.0
    assert FramingEstimates(path).hours(1.7) == 1.375


def test_damaged_estimates_fall_back_to_the_known_ones(tmp_path):
    path = tmp_path / "f.json"
    path.write_text("{broken")
    assert FramingEstimates(str(path)).hours(1.7) == 1.375


def test_new_framed_targets_start_at_the_estimate(tmp_path):
    found = apply_estimates(targets_from_plan(PLAN), FramingEstimates(str(tmp_path / "f.json")))
    assert found[0]["min_session_hours"] == 1.375
    assert "min_session_hours" not in found[1]


def test_unknown_size_keeps_the_fallback(tmp_path):
    plan = json.loads(json.dumps(PLAN))
    plan["list"][0]["mosaic"]["scale"] = 2.5
    found = apply_estimates(targets_from_plan(plan), FramingEstimates(str(tmp_path / "f.json")))
    assert found[0]["min_session_hours"] == 4.0


def test_reimport_does_not_change_a_chosen_shortest_run(tmp_path):
    queue = TargetQueue(str(tmp_path / "t.json"))
    estimates = FramingEstimates(str(tmp_path / "f.json"))
    queue.merge(apply_estimates(targets_from_plan(PLAN), estimates))
    first = queue.all()[0]
    queue.update(first.id, min_session_hours=6.0)
    queue.merge(apply_estimates(targets_from_plan(PLAN), estimates))
    assert queue.all()[0].min_session_hours == 6.0


def test_page_flags_a_shortest_run_under_the_estimate(page, monkeypatch):
    _, queue_web, _ = page
    monkeypatch.setattr(
        queue_web.time, "time", lambda: datetime(2026, 11, 6, 2, tzinfo=timezone.utc).timestamp()
    )
    queue_web._queue().merge(targets_from_plan(PLAN))
    target = queue_web._queue().all()[0]
    queue_web._queue().update(target.id, min_session_hours=1.0)
    rows = {r["target"].name: r for r in queue_web._context()["rows"]}
    assert rows["NGC 6960"]["short"] is True and rows["NGC 6960"]["estimate"] == 1.375
    assert rows["M 42"]["short"] is False and rows["M 42"]["estimate"] is None
    queue_web._queue().update(target.id, min_session_hours=1.375)
    assert queue_web._context()["rows"][0]["short"] is False


def test_saving_under_the_estimate_warns(page, monkeypatch):
    client, queue_web, _ = page
    import roof.web

    shown = _NoPage()
    monkeypatch.setattr(roof.web, "_front", lambda: shown)
    queue_web._queue().merge(targets_from_plan(PLAN))
    target = queue_web._queue().all()[0]
    body = f"id={target.id}&goal_hours=9&min_session_hours=1&prior_hours=0&priority=normal&enabled=on"
    client.simulate_post("/targets/update", headers=FORM, body=body)
    assert "1.375 h estimate" in shown.message


def test_automation_learns_the_estimate_only_while_stacking(tmp_path):
    from tests.test_roof import Rig

    rig = Rig(tmp_path)
    estimates = FramingEstimates(str(tmp_path / "f.json"))
    rig.auto._framing_estimates = estimates
    rig.auto._session_log = SessionLog(str(tmp_path / "s.json"))
    rig.set_scope(view_state="working", view_stage="AutoGoto", mosaic_scale=2.2, mosaic_estimate_h=3.0)
    rig.tick()
    assert estimates.hours(2.2) is None
    rig.set_scope(view_stage="Stack")
    rig.tick(10)
    assert estimates.hours(2.2) == 3.0


def test_scope_read_carries_the_framing_estimate():
    reply = {"setting": {"mosaic": {"scale": 1.7, "estimated_hours": 1.375}}}
    device = SimpleNamespace(
        is_connected=True,
        device_name="scope",
        schedule={"state": "stopped", "list": [], "item_number": 0},
        scheduler_thread=None,
        send_message_param_sync=lambda data: {
            "result": reply if data["method"] == "get_device_state" else {}
        },
    )
    state = RealScope(lambda: device).read()
    assert (state.mosaic_scale, state.mosaic_estimate_h) == (1.7, 1.375)
