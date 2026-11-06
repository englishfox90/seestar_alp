"""The Tonight screen: state mapping, alarms, Stop, the Night Line and its polling."""

import re
from types import SimpleNamespace

import falcon
import falcon.testing
import pytest

import front.app as front_app
from roof import dashboard, planner, tonight, web as roof_web

NOW = 1793938440.0
WINDOW = {"sunset": NOW - 4 * 3600, "sunrise": NOW + 9 * 3600, "dark_start": NOW - 3 * 3600, "dark_end": NOW + 7 * 3600}
IMAGING = {
    "safe": True, "safety_detail": "safe", "dark": True, "sun_deg": -40.0, "sun_max_deg": -18.0,
    "connected": True, "parked": False, "enabled": True, "schedule": "Good.json", "phase": "imaging",
    "sched_state": "working", "view_state": "working", "view_stage": "Stack", "target": "NGC 6992",
    "view_target": "NGC 6992", "stacked": 412, "dropped": 37, "exposure_s": 20.0, "stack_lapse_s": 9000.0,
    "loop_stale": False, "loop_age_s": 3.0, "alarm": "", "override_roof": False, "held": False,
    "completed": False, "foreign": False, "dry_run": False, "log": [], "note": "",
}
PARKED = {
    **IMAGING, "phase": "idle", "parked": True, "sched_state": "stopped", "view_state": "cancel",
    "view_stage": "", "target": "", "view_target": "", "stacked": 0, "dropped": 0,
}
DAY = {**PARKED, "dark": False, "sun_deg": 25.0}
PARK_LINE = "2026-11-05 22:14:03  parked: firmware reports mount closed"


def clock(ts):
    return f"t{int(ts - NOW)}"


def state(status, events=None, plan=None, stack=None, **kw):
    plan = {"window": WINDOW, **(plan or {})}
    return tonight.tonight_state(status, events, stack, plan, now=NOW, clock=clock, **kw)


# (name, status, events, plan, key, sentence, hero, stop offered)
CASES = [
    ("day", DAY, None, {"next": {"name": "NGC 6992", "clock": "18:12"}}, "idle", "Starts 18:12 with NGC 6992", "plan", False),
    ("day, roof closed", {**DAY, "safe": False}, None, {"next": {"name": "NGC 6992", "clock": "18:12"}}, "idle", "Starts 18:12 with NGC 6992 if the roof reads safe", "plan", False),
    ("waiting", {**PARKED, "dark": False, "sun_deg": -9.2}, None, None, "waiting_dark", "Waiting for dark, sun \N{MINUS SIGN}9\N{DEGREE SIGN}", "pier", False),
    ("opening", {**IMAGING, "phase": "opening", "parked": True, "view_state": ""}, None, None, "moving", "Opening the arm", "pier", True),
    ("slewing", {**IMAGING, "view_stage": "AutoGoto"}, None, None, "moving", "Moving to NGC 6992", "pier", True),
    ("solving", {**IMAGING, "view_stage": "AutoGoto"}, {"PlateSolve": {"state": "working"}}, None, "moving", "Plate solving NGC 6992", "pier", True),
    ("autofocus", {**IMAGING, "view_stage": "AutoFocus"}, {"FocuserMove": {"position": 1172}}, None, "autofocus", "Focusing, position 1172", "focus", True),
    ("darks", {**IMAGING, "view_stage": "AutoFocus"}, {"DarkLibrary": {"state": "working", "percent": 40.0}}, None, "darks", "Taking darks, 40%", "stack", True),
    ("stacking", IMAGING, None, None, "stacking", "Stacking NGC 6992", "stack", True),
    ("held", {**PARKED, "held": True}, None, None, "held", "Held. 7 h of dark left", "stack", False),
    ("finished", {**PARKED, "completed": True}, None, {"earned_hours": 6.2}, "finished", "Finished. 6.2 h earned", "stack", False),
    ("roof unsafe, parking", {**IMAGING, "safe": False, "phase": "parking"}, None, None, "roof_unsafe", "Roof unsafe. Parking", "pier", False),
    ("now parked", {**PARKED, "safe": False, "log": [PARK_LINE]}, None, None, "now_parked", None, "pier", False),
    ("parking", {**IMAGING, "phase": "parking"}, None, None, "parking", "Parking", "pier", False),
    ("offline", {**PARKED, "connected": False, "parked": None, "last_connected_at": NOW - 60}, None, None, "offline", "Scope offline, last seen t-60", "stack", False),
    ("loop stale", {**IMAGING, "loop_stale": True, "loop_age_s": 600.0}, None, None, "offline", "Automation stopped reporting, last report t-600", "stack", False),
    ("arm open", {**PARKED, "parked": False}, None, None, "arm_open", "Arm open, not imaging", "pier", True),
    ("elsewhere", {**PARKED, "foreign": True, "view_state": "working", "view_stage": "Scenery"}, None, None, "elsewhere", "A session started elsewhere is running", "stack", True),
]


@pytest.mark.parametrize("name, status, events, plan, key, sentence, hero, stop", CASES, ids=[c[0] for c in CASES])
def test_every_state(name, status, events, plan, key, sentence, hero, stop):
    result = state(status, events, plan)
    assert result["key"] == key
    assert result["hero"] == hero
    if sentence:
        assert result["sentence"] == sentence
    assert result["stop"] is stop
    assert (result["action"] or {}).get("kind") == "stop" or not stop


def test_now_parked_gives_the_real_time_and_says_which_thing_is_safe():
    result = state({**PARKED, "safe": False, "log": ["junk", PARK_LINE]})
    assert re.fullmatch(r"Parked t-?\d+, scope safe", result["sentence"])
    assert not result["bands"], "the band relaxes"
    assert any("Roof reads unsafe" in n["text"] for n in result["notices"])


def test_now_parked_invents_no_time_without_a_log_line():
    assert state({**PARKED, "safe": False})["sentence"] == "Parked, scope safe"


def test_offline_invents_no_last_seen():
    assert state({**PARKED, "connected": False})["sentence"] == "Scope offline"


@pytest.mark.parametrize(
    "changes, sentence",
    [
        ({"phase": "imaging"}, "Roof unsafe. Arm still open"),
        ({"enabled": False}, "Roof unsafe. Automation off, will not park"),
        ({"connected": False}, "Roof unsafe. Scope offline, cannot park"),
        ({"parked": None}, "Roof unsafe. Arm position unknown"),
    ],
)
def test_roof_unsafe_only_says_parking_while_parking(changes, sentence):
    result = state({**IMAGING, "safe": False, **changes})
    assert result["key"] == "roof_unsafe"
    assert result["sentence"] == sentence
    assert "Parking" not in result["sentence"]


def test_roof_unsafe_says_it_in_the_band_with_the_sensors_reason():
    result = state({**IMAGING, "safe": False, "phase": "parking", "safety_detail": "read failed: timeout"})
    assert result["sentence_in_band"]
    assert result["bands"][-1] == {"headline": "Roof unsafe. Parking", "line": "Roof sensor: read failed: timeout"}


def test_roof_closed_by_day_is_not_an_event():
    assert state({**DAY, "safe": False})["bands"] == []


def test_morning_twilight_is_not_waiting_for_dark():
    status = {**PARKED, "dark": False, "sun_deg": -9.0}
    after = tonight.tonight_state(status, plan={"window": {**WINDOW, "dark_end": NOW - 60}}, now=NOW, clock=clock)
    assert after["key"] == "idle"


@pytest.mark.parametrize(
    "changes, words",
    [
        ({"enabled": False}, "Automation off"),
        ({"schedule": ""}, "No schedule selected"),
        ({"sun_deg": None}, "No site location set"),
    ],
)
def test_idle_never_promises_a_start_that_cannot_happen(changes, words):
    plan = {"next": {"name": "NGC 6992", "clock": "18:12"}}
    assert words in state({**DAY, **changes}, plan=plan)["sentence"]


def test_note_is_used_after_dark():
    status = {**PARKED, "note": "waiting: roof must read safe for 120s"}
    assert state(status)["sentence"] == "Roof must read safe for 120s"


def test_held_after_dawn():
    plan = {"window": {**WINDOW, "dark_end": NOW - 60}}
    assert tonight.tonight_state({**PARKED, "held": True}, plan=plan, now=NOW, clock=clock)["sentence"] == "Held until the next night"


def test_held_links_to_the_roof_page_and_no_start_anywhere():
    action = state({**PARKED, "held": True})["action"]
    assert action == {"kind": "link", "label": "Open the Roof page", "route": "roof"}


# ---- alarms outrank everything ------------------------------------------------------


@pytest.mark.parametrize("name, status, events, plan, key, sentence, hero, stop", CASES, ids=[c[0] for c in CASES])
def test_alarm_is_never_hidden_in_any_state(name, status, events, plan, key, sentence, hero, stop):
    bands = state({**status, "alarm": "PARK NOT CONFIRMED"}, events, plan)["bands"]
    assert bands[0 if not status.get("loop_stale") else 1]["line"] == "PARK NOT CONFIRMED"


@pytest.mark.parametrize("name, status, events, plan, key, sentence, hero, stop", CASES, ids=[c[0] for c in CASES])
def test_override_is_never_hidden_in_any_state(name, status, events, plan, key, sentence, hero, stop):
    bands = state({**status, "override_roof": True}, events, plan)["bands"]
    assert any("ignoring the roof" in b["headline"] for b in bands)


def test_bands_keep_their_order():
    status = {**IMAGING, "safe": False, "phase": "parking", "loop_stale": True, "alarm": "bad", "override_roof": True}
    headlines = [b["headline"] for b in state(status)["bands"]]
    assert "stopped reporting" in headlines[0]
    assert headlines[1] == "Automation alarm"
    assert "ignoring the roof" in headlines[2]


def test_loop_stale_outranks_what_the_stale_snapshot_says():
    result = state({**IMAGING, "safe": False, "phase": "parking", "loop_stale": True})
    assert result["key"] == "offline"
    assert result["tone"] == "danger"


def test_loop_never_reported():
    result = state({**IMAGING, "loop_stale": True, "loop_age_s": None})
    assert result["sentence"] == "Automation has not reported yet"


def test_override_keeps_the_true_state_rather_than_claiming_parking():
    result = state({**IMAGING, "safe": False, "override_roof": True})
    assert result["key"] == "stacking"
    assert result["stop"]


def test_alarm_does_not_take_stop_away():
    assert state({**IMAGING, "alarm": "bad"})["stop"]


def test_status_line_matches_the_legacy_badge():
    assert [s["text"] for s in tonight.roof_status({"loop_stale": True, "connected": False})] == ["Loop stopped"]
    assert [s["text"] for s in tonight.roof_status({"alarm": "x", "safe": True, "connected": True})] == ["Roof alarm"]
    assert [s["text"] for s in tonight.roof_status({"safe": False, "connected": False, "dry_run": True})] == [
        "Roof unsafe", "Scope offline", "Dry run",
    ]


# ---- Stop ------------------------------------------------------------------------------


def test_stop_needs_a_loop_to_hear_it():
    assert state({**IMAGING, "loop_stale": True})["action"] is None


def test_stop_needs_a_connected_scope():
    assert state({**IMAGING, "safe": False, "connected": False})["action"] is None


def test_stop_is_hidden_while_parking():
    assert state({**IMAGING, "safe": False, "phase": "parking"})["action"] is None


def test_stop_stays_disabled_after_a_press_until_the_loop_answers():
    result = state(IMAGING, stop_requested_at=NOW - 10)
    assert result["action"]["kind"] == "stop" and result["action"]["stopping"]
    assert result["stop"] is False, "an open sheet closes"
    later = state(IMAGING, stop_requested_at=NOW - tonight.STOP_PENDING_S - 1)
    assert later["stop"] is True, "a request the loop never acted on can be retried"


def test_stop_is_not_offered_for_another_scope():
    result = state(IMAGING, here=False)
    assert result["action"] is None
    assert "the scope roof automation runs" in result["notices"][0]["text"]


def test_dry_run_is_said():
    assert any("Dry run" in n["text"] for n in state({**IMAGING, "dry_run": True})["notices"])


def test_automation_off_during_a_session_says_it_wont_park():
    assert any("will not park" in n["text"] for n in state({**IMAGING, "enabled": False})["notices"])


def test_a_failed_step_is_noticed():
    events = {"AutoFocus": {"state": "fail", "error": "no star is detected"}}
    assert any("no star is detected" in n["text"] for n in state(IMAGING, events)["notices"])


# ---- metrics ------------------------------------------------------------------------------


def test_stacking_metrics():
    stack = tonight.stack_summary(IMAGING, goal={"have": 3.4, "goal": 10.0, "partial": 0.4})
    metrics = state(IMAGING, stack=stack)["metrics"]
    assert [m["label"] for m in metrics] == ["Earned", "Kept", "Goal"]
    assert metrics[0]["value"] == "2h 17m"
    assert metrics[1]["value"] == "92%" and metrics[1]["hint"] == "412 of 449 frames"
    assert (metrics[2]["value"], metrics[2]["unit"]) == ("3.4", "of 10 h")
    assert "0.4 h not counted" in metrics[2]["hint"]


def test_metrics_show_a_dash_rather_than_a_guess():
    stack = tonight.stack_summary({**IMAGING, "stacked": 0, "dropped": 0, "exposure_s": 0})
    values = [m["value"] for m in state(IMAGING, stack=stack)["metrics"]]
    assert values == ["\N{EM DASH}", "\N{EM DASH}"]


def test_earned_survives_a_gap_between_exposures():
    rows = [{"target": "ngc 6992", "exposure_s": 20.0}]
    assert tonight.stack_summary({**IMAGING, "exposure_s": 0}, rows)["earned"] == "2h 17m"


@pytest.mark.parametrize("key_status", [DAY, {**IMAGING, "view_stage": "AutoGoto"}, {**PARKED, "held": True}])
def test_only_stacking_has_metrics(key_status):
    assert state(key_status, stack=tonight.stack_summary(IMAGING))["metrics"] == []


def test_night_summary_counts_only_that_night():
    rows = [
        {"name": "NGC 6992_1", "night": "n", "stacked": 360, "exposure_s": 20.0},
        {"name": "M 42", "night": "other", "stacked": 999, "exposure_s": 20.0},
    ]
    summary = tonight.night_summary(rows, "n")
    assert summary["hours"] == 2.0
    assert summary["targets"] == [{"name": "NGC 6992", "hours": "2 h"}]


# ---- Night Line -----------------------------------------------------------------------------


def test_night_line_percentages_and_label():
    blocks = [
        {"name": "IC 1805", "start": NOW - 3 * 3600, "end": NOW - 3600, "progress": "done"},
        {"name": "NGC 6992", "start": NOW - 3600, "end": NOW + 3600, "progress": "now"},
        {"name": "M 42", "start": NOW + 3600, "end": NOW + 2 * 3600},
    ]
    line = tonight.night_line(WINDOW, blocks, NOW, clock)
    assert line["available"]
    assert line["dark"] == {"start": 7.69, "end": 84.62}
    assert [b["state"] for b in line["blocks"]] == ["done", "now", "next"]
    assert line["now"] == 30.77
    assert "NGC 6992" in line["label"] and "now" in line["label"]


def test_night_line_named_schedule_has_only_the_dark_window():
    line = tonight.night_line(WINDOW, [], NOW, clock)
    assert line["blocks"] == [] and line["dark"]


def test_night_line_has_no_marker_by_day():
    assert tonight.night_line(WINDOW, [], WINDOW["sunset"] - 60, clock)["now"] is None


def test_night_line_clamps_blocks_to_the_axis():
    block = {"name": "x", "start": WINDOW["sunset"] - 9999, "end": WINDOW["sunrise"] + 9999}
    drawn = tonight.night_line(WINDOW, [block], NOW, clock)["blocks"][0]
    assert (drawn["start"], drawn["end"]) == (0.0, 100.0)


def test_night_line_without_a_site_says_so():
    line = tonight.night_line(None)
    assert not line["available"] and "site" in line["label"]


def test_night_line_labels_at_most_three_on_a_phone():
    span = (WINDOW["sunrise"] - WINDOW["sunset"]) * 0.2
    blocks = [{"name": str(i), "start": WINDOW["sunset"] + i * span, "end": WINDOW["sunset"] + (i + 1) * span} for i in range(5)]
    tiers = [b["tier"] for b in tonight.night_line(WINDOW, blocks, NOW, clock)["blocks"]]
    assert tiers.count(1) == 3
    tiny = [{"name": "t", "start": WINDOW["sunset"], "end": WINDOW["sunset"] + 60}]
    assert tonight.night_line(WINDOW, tiny, NOW, clock)["blocks"][0]["tier"] is None


def test_night_times_matches_the_planners_dark_window():
    cfg = planner.PlanConfig(lat=31.5, lon=-99.37, sun_max_deg=-18.0)
    plan = planner.plan_night([], {}, "2026-11-05", cfg)
    times = planner.night_times("2026-11-05", 31.5, -99.37, -18.0)
    assert times["dark_start"] == plan.dark_start
    assert times["dark_end"] == plan.dark_end
    assert times["sunset"] < times["dark_start"] < times["dark_end"] < times["sunrise"]


# ---- the page and its polling -----------------------------------------------------------------


def _fail(*a, **kw):
    raise AssertionError("this must not ask the scope")


TELESCOPES = [{"device_num": 1, "name": "Alpha", "ip_address": "10.0.0.1"}]
CONTEXT = {
    "root": "/1", "telescope": TELESCOPES[0], "telescopes": TELESCOPES, "experimental": True,
    "platform": "raspberry_pi", "partial_path": "dashboard",
}


@pytest.fixture
def client(monkeypatch):
    holder = SimpleNamespace(status=dict(IMAGING), actions=[], stops=[])
    automation = SimpleNamespace(
        status=lambda: holder.status, start=lambda: None, request=holder.stops.append
    )
    monkeypatch.setattr(roof_web, "get_automation", lambda: automation)
    monkeypatch.setattr(dashboard, "get_automation", lambda: automation)
    monkeypatch.setattr(dashboard, "session_log", lambda: None)
    monkeypatch.setattr(tonight, "_stop_requested_at", None)
    monkeypatch.setattr(dashboard, "_stores", {})
    monkeypatch.setattr(dashboard, "_pier_store", dashboard.PierStore())
    monkeypatch.setattr(front_app, "method_sync", _fail)
    monkeypatch.setattr(front_app, "get_device_state", _fail)

    def action(name, *a, **kw):
        holder.actions.append(name)
        return {"Value": {"result": {}}}

    monkeypatch.setattr(front_app, "do_action_device", action)
    monkeypatch.setattr(front_app, "get_context", lambda *a: CONTEXT)
    app = falcon.App()
    roof_web.install(app)
    test_client = falcon.testing.TestClient(app)
    test_client.holder = holder
    return test_client


def _get(client, path):
    result = client.simulate_get(path)
    assert result.status_code == 200, result.text
    return result.content.decode()


def test_state_never_messages_the_scope(client, monkeypatch):
    monkeypatch.setattr(front_app, "get_context", _fail)
    monkeypatch.setattr(front_app, "check_needs_auth", _fail)
    for _ in range(3):
        html = _get(client, "/1/tonight/state")
    assert "Stacking NGC 6992" in html
    assert set(client.holder.actions) == {"get_event_state"}, "answered from ALP's own cache"


def test_state_does_not_replan_on_every_poll(client, monkeypatch):
    client.holder.status = {**IMAGING, "uses_target_list": True, "target_index": 1}
    monkeypatch.setattr(tonight, "_plan_cache", {})
    calls = []

    def plan(*a):
        calls.append(1)
        return {"rows": [], "skipped": []}

    monkeypatch.setattr(tonight, "_list_plan", plan)
    monkeypatch.setattr(dashboard, "target_queue", lambda: SimpleNamespace(all=lambda: []))
    for _ in range(3):
        _get(client, "/1/tonight/state")
    assert len(calls) == 1


def test_state_updates_every_part_out_of_band(client):
    html = _get(client, "/1/tonight/state")
    for part in ("t-bands", "t-progress", "t-night", "t-plan", "t-session-details", "ssc-status", "t-updated-time"):
        assert f'id="{part}" hx-swap-oob="innerHTML"' in html, part


def test_state_escapes_what_it_shows(client):
    client.holder.status = {**IMAGING, "alarm": "<script>x</script>", "target": "<b>y</b>"}
    html = _get(client, "/1/tonight/state")
    assert "<script>x" not in html and "&lt;script&gt;x" in html
    assert "<b>y" not in html


def test_hardware_only_loads_with_details_open(client):
    html = _get(client, "/1/dashboard")
    trigger = re.search(r'id="t-hardware".*?hx-trigger="([^"]+)"', html, re.S).group(1)
    assert "load" not in trigger
    assert trigger.startswith("toggle[")
    assert "every 30s" in trigger and ".open" in trigger.split("every 30s")[1]


def test_hardware_fragment(client, monkeypatch):
    monkeypatch.setattr(front_app, "get_device_state", lambda t: {"Battery %": 4, "Mount Mode": "<b>Eq</b>"})
    html = _get(client, "/1/tonight/hardware")
    assert "4%" in html and "&lt;b&gt;Eq" in html and 'data-tone="danger"' in html
    assert "text-danger" not in html


def test_hardware_keeps_the_last_readings_when_the_scope_does_not_answer(client, monkeypatch):
    monkeypatch.setattr(front_app, "get_device_state", lambda t: {"Info": "Unable to get stats."})
    assert client.simulate_get("/1/tonight/hardware").status_code == 204


def test_page_has_the_shell_and_no_bootstrap(client):
    html = _get(client, "/1/dashboard")
    assert "<title>Tonight - SSC</title>" in html
    assert "<h1" in html and ">Tonight</h1>" in html
    assert "public/ssc/ssc.css" in html and "public/ssc/theme.js" in html
    assert html.index("ssc/theme.js") < html.index("ssc.css"), "theme is set before paint"
    assert "bootstrap" not in html.lower()
    assert "data-bs-toggle" not in html
    classes = {name for value in re.findall(r'class="([^"]*)"', html) for name in value.split()}
    assert not classes & {"btn", "card", "row", "navbar", "dropdown-item", "alert", "badge"}
    assert "<style" not in html


@pytest.mark.parametrize(
    "status, shown",
    [(IMAGING, True), ({**IMAGING, "phase": "parking"}, False), ({**IMAGING, "loop_stale": True}, False), (DAY, False)],
)
def test_page_shows_stop_only_where_it_can_stop(client, status, shown):
    client.holder.status = status
    html = _get(client, "/1/dashboard")
    assert ('data-open-sheet="t-stop-sheet"' in html) is shown


def test_confirm_sheet_is_a_native_dialog_outside_polled_regions(client):
    html = _get(client, "/1/dashboard")
    assert '<dialog class="ssc-sheet" id="t-stop-sheet"' in html
    assert html.index('id="t-now"') < html.index("</main>") < html.index('id="t-stop-sheet"')
    assert "Stop &amp; park" in html and "Keep imaging" in html
    assert 'name="return_to" value="dashboard"' in html
    assert "confirm(" not in html


def test_stop_from_tonight_returns_to_tonight_without_a_flash(client):
    result = client.simulate_post(
        "/1/roof/stop",
        body="return_to=dashboard",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert result.status_code == 302
    assert result.headers["location"].endswith("/1/dashboard")
    assert client.holder.stops == ["stop"]
    assert tonight._stop_requested_at is not None
    assert "Stopping" in _get(client, "/1/tonight/state")


def test_stop_from_the_roof_page_behaves_as_before(client, monkeypatch):
    monkeypatch.setattr(front_app, "messages", [])
    result = client.simulate_post("/1/roof/stop")
    assert result.headers["location"].endswith("/1/roof")
    assert client.holder.stops == ["stop"]
    assert front_app.messages == ["Stop + Park requested; see the log below."]


def test_stop_still_refuses_another_origin(client):
    result = client.simulate_post("/1/roof/stop", headers={"Origin": "http://evil.example"})
    assert result.status_code == 403
    assert client.holder.stops == []


def test_no_start_on_tonight(client):
    client.holder.status = {**PARKED, "held": True}
    html = _get(client, "/1/dashboard")
    assert "roof/start" not in html
    assert ">Start<" not in html


def test_no_browser_clock_is_used():
    from pathlib import Path

    script = (Path(front_app.__file__).parent / "public" / "ssc" / "tonight.js").read_text()
    assert "toLocale" not in script
    assert "new Date" not in script


@pytest.mark.parametrize(
    "hero, stack, pier, front, layout",
    [
        ("stack", True, True, "stack", "side"),
        ("stack", False, True, "pier", "below"),
        ("stack", True, False, "stack", "below"),
        ("pier", True, True, "pier", "below"),
        ("pier", True, False, "stack", "below"),
        ("pier", False, False, "none", "thumbs"),
        ("plan", False, True, "none", "thumbs"),
        ("focus", True, True, "none", "thumbs"),
    ],
)
def test_first_layout_never_leads_with_a_missing_image(hero, stack, pier, front, layout):
    images = {"stack": {} if not stack else {"frame": 1}, "pier": {} if not pier else {"frame": "p"}}
    assert tonight.first_layout(hero, images) == {"front": front, "layout": layout}


# ---- the nav ------------------------------------------------------------------------------------


def _hrefs(html):
    return set(re.findall(r'href="(/[^"]*)"', html))


def _ssc_nav(**extra):
    context = {
        "root": "/1", "root_path": "", "telescope": TELESCOPES[0], "telescopes": TELESCOPES,
        "experimental": True, "platform": "raspberry_pi", "partial_path": "dashboard",
        "roof_lines": [], **extra,
    }
    return front_app.fetch_template("ssc/nav.html").render(**context)


def test_new_nav_reaches_exactly_what_the_legacy_nav_does():
    context = {
        "root": "/1", "root_path": "", "telescope": TELESCOPES[0], "telescopes": TELESCOPES,
        "experimental": True, "platform": "raspberry_pi", "partial_path": "dashboard", "uitheme": "dark",
    }
    legacy = front_app.fetch_template("nav.html").render(**context)
    assert _hrefs(_ssc_nav()) == _hrefs(legacy)


@pytest.mark.parametrize(
    "route,partial_path",
    [("dashboard", "dashboard")]
    + [(r, "targets") for r in ("targets", "roof", "schedule", "framed_mosaic", "planning", "mosaic")]
    + [(r, "live") for r in ("live", "goto", "image", "startup", "")]
    + [(r, "settings") for r in ("settings", "config", "command", "stats", "guestmode", "support", "platform-rpi", "system")],
)
def test_every_route_is_reachable_from_the_new_nav(route, partial_path):
    assert f'href="/1/{route}"' in _ssc_nav(partial_path=partial_path)


def test_conditional_routes_follow_the_same_rules():
    for partial_path in ("targets", "settings"):
        html = _ssc_nav(experimental=False, platform="mac", partial_path=partial_path)
        assert "/1/planning" not in html and "/1/platform-rpi" not in html


def test_new_nav_has_the_switcher_federation_red_and_labels():
    html = _ssc_nav()
    assert "Seestar Federation" in html and 'href="/0/dashboard"' in html
    assert "data-red-toggle" in html and "Red night mode" in html
    assert "data-dark-toggle" not in html
    assert 'id="ssc-status"' in html and 'href="/1/roof"' in html
    assert re.search(r"</svg>Tonight</a>", html)
    assert ">Tonight</a>" in html


def test_webui_accent_is_a_variable_and_gets_a_readable_partner(client, monkeypatch):
    assert tonight._on_accent("#ffffff") == "black"
    assert tonight._on_accent("#000000") == "white"
    assert tonight._on_accent("teal") is None
    monkeypatch.setattr(front_app, "get_context", lambda *a: {**CONTEXT, "webui_accent_color": "#ffffff", "webui_font_family": "Avenir", "webui_text_color": "#123456"})
    html = _get(client, "/1/dashboard")
    assert "--ssc-webui-accent: #ffffff; --ssc-webui-on-accent: black; --ssc-webui-font: Avenir;" in html
    assert "#123456" not in html, "text colour is for legacy pages only"
