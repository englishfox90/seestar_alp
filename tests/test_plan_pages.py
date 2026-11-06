"""Targets and Automation on the shell: every control keeps its route and fields."""

import re
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import falcon
import falcon.testing
import pytest

import front.app as front_app
from roof import queue_web, schedule_tools
from roof import web as roof_web
from roof.config import Settings
from roof.sessions import SessionLog
from tests.test_design_rules import BOOTSTRAP

FORM = {"Content-Type": "application/x-www-form-urlencoded"}
TELESCOPES = [{"device_num": 1, "name": "Alpha", "ip_address": "10.0.0.1"}]
CONTEXT = {
    "root": "/1", "telescope": TELESCOPES[0], "telescopes": TELESCOPES, "experimental": True,
    "platform": "raspberry_pi", "partial_path": "", "needs_auth_warning": False,
}
IMAGING = {
    "enabled": True, "dry_run": False, "safe": True, "safety_detail": "safe", "dark": True, "sun_deg": -40.0,
    "sun_max_deg": -18.0, "phase": "imaging", "connected": True, "scope_name": "S50", "parked": False,
    "sched_state": "working", "target_index": 1, "target_total": 2, "target": "M 42", "tracking": True,
    "mosaic_scale": 1.0, "foreign": False, "override_roof": False, "held": False, "completed": False,
    "resume_item": None, "alarm": "", "note": "", "loop_stale": False, "loop_age_s": 3.0, "uptime_s": 3700,
    "headline": "Imaging M 42", "dew_enabled": False, "targets": [], "log": ["22:01 Start pressed"],
}
DAY_PARKED = {
    **IMAGING, "safe": False, "safety_detail": "IsSafe is not true", "dark": False, "phase": "idle", "parked": True,
    "sched_state": "stopped", "target": "", "target_index": 0, "headline": "Parked, waiting for dark",
}


class _Forms(HTMLParser):
    """Forms with their action, method and fields. Also every element, by id."""

    def __init__(self):
        super().__init__()
        self.forms, self.ids, self.classes, self._current = [], [], set(), None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        self.classes.update((attrs.get("class") or "").split())
        if tag == "form":
            self._current = {"action": attrs.get("action"), "method": attrs.get("method"), "fields": {}, "id": attrs.get("id")}
            self.forms.append(self._current)
        elif self._current is not None and tag in ("input", "select"):
            if attrs.get("name"):
                self._current["fields"].setdefault(attrs["name"], attrs)

    def handle_endtag(self, tag):
        if tag == "form":
            self._current = None


def _parse(html):
    parser = _Forms()
    parser.feed(html)
    return parser


def _form(html, action):
    matches = [f for f in _parse(html).forms if f["action"] == action]
    assert matches, f"no form posts to {action}"
    assert all(f["method"] == "post" for f in matches)
    return matches[0]


@pytest.fixture
def rig(monkeypatch, tmp_path):
    state = SimpleNamespace(
        status=dict(IMAGING), requests=[],
        settings=Settings(lat=31.5, lon=-99.37, device_num=1, enabled=True, dry_run=False, schedule=""),
    )
    automation = SimpleNamespace(status=lambda: state.status, start=lambda: None, request=state.requests.append)
    monkeypatch.setattr(roof_web, "get_automation", lambda: automation)
    monkeypatch.setattr(roof_web.roof_config, "load", lambda: state.settings)
    monkeypatch.setattr(roof_web.focus, "listen", lambda: None)
    monkeypatch.setattr(roof_web.tonight, "note_stop_requested", lambda: None)
    log = SessionLog(str(tmp_path / "s.json"))
    monkeypatch.setattr(queue_web, "session_log", lambda: log)
    monkeypatch.setattr(queue_web, "_base", lambda: str(tmp_path))
    monkeypatch.setattr(schedule_tools, "list_schedules", lambda: [])
    monkeypatch.setattr(front_app, "get_context", lambda *a: dict(CONTEXT))
    monkeypatch.setattr(front_app, "get_device_model", lambda n: "S50", raising=False)
    app = falcon.App()
    app.req_options.auto_parse_form_urlencoded = False
    roof_web.install(app)
    state.client = falcon.testing.TestClient(app)
    return state


def _get(rig, path, **kw):
    result = rig.client.simulate_get(path, **kw)
    assert result.status_code == 200, result.text
    return result.content.decode("utf-8")


def _no_bootstrap(html):
    classes = _parse(html).classes
    found = {name for name in classes if BOOTSTRAP.fullmatch(name)}
    assert not found, found
    assert "bootstrap" not in html.lower()


# ---- Automation ------------------------------------------------------------------------------


def test_automation_is_on_the_shell(rig):
    html = _get(rig, "/1/roof")
    assert "<title>Automation - SSC</title>" in html and ">Automation</h1>" in html
    assert "public/ssc/ssc.css" in html
    _no_bootstrap(html)
    assert "<style" not in html
    assert "confirm(" not in html


def test_automation_keeps_every_control_and_route(rig):
    html = _get(rig, "/1/roof")
    assert _form(html, "/1/roof/select")["fields"]["schedule"]["id"] == "roof-schedule"
    assert _form(html, "/1/roof/enable")["fields"]["enabled"]["value"] == "false"
    assert "Disable automation" in html
    assert _form(html, "/1/roof/start")["fields"] == {}
    assert _form(html, "/1/roof/stop")["fields"] == {}
    assert "required" in _form(html, "/1/roof/capture")["fields"]["name"]
    assert 'hx-get="/1/roof/status"' in html and 'id="roof-status"' in html


def test_enable_button_offers_the_opposite_state(rig):
    rig.settings = replace(rig.settings, enabled=False)
    html = _get(rig, "/1/roof")
    assert _form(html, "/1/roof/enable")["fields"]["enabled"]["value"] == "true"
    assert "Enable automation" in html


@pytest.mark.parametrize("route, command", [("start", "start"), ("stop", "stop")])
def test_the_sheets_forms_send_the_same_commands(rig, route, command):
    result = rig.client.simulate_post(f"/1/roof/{route}", headers=FORM, body="")
    assert result.status_code == 302
    assert result.headers["location"].endswith("/1/roof")
    assert rig.requests == [command]


def test_start_needs_its_sheet_and_the_sheet_keeps_the_roof_warning(rig):
    html = _get(rig, "/1/roof")
    main, sheets = html.split("</main>")
    assert 'data-open-sheet="roof-start-sheet"' in main
    assert 'action="/1/roof/start"' not in main, "Start goes through the sheet"
    assert "Start ignores the roof status" in main, "visible before it is pressed"
    sheet = sheets[sheets.index('id="roof-start-sheet"'):]
    assert "This ignores the roof status" in sheet
    assert "It will not park for the roof until the roof next reads safe" in sheet
    assert "Start, ignoring the roof" in sheet
    assert re.search(r'plan-override"[^>]*data-open-sheet', main), "not styled as a routine action"
    assert re.search(r'<button[^>]*autofocus[^>]*>Don\'t start', sheet), "the safe choice has focus"


def test_stop_matches_tonight(rig):
    html = _get(rig, "/1/roof")
    main, sheets = html.split("</main>")
    assert '<button class="ssc-button ssc-stop" type="button" data-open-sheet="roof-stop-sheet">Stop &amp; park</button>' in main
    assert 'action="/1/roof/stop"' not in main
    sheet = sheets[sheets.index('id="roof-stop-sheet"'):]
    assert 'class="ssc-button ssc-stop" data-solid="1"' in sheet and "Keep imaging" in sheet
    assert "return_to" not in sheet, "so it comes back here, not to Tonight"


def test_sheets_sit_outside_every_polled_region(rig):
    html = _get(rig, "/1/roof")
    assert html.index('id="roof-status"') < html.index("</main>") < html.index('id="roof-stop-sheet"')
    assert html.index("</main>") < html.index('id="roof-start-sheet"')


def test_live_is_calm_and_dry_run_warns(rig):
    html = _get(rig, "/1/roof")
    assert '<p class="ssc-notice plan-mode" id="roof-mode" data-tone="neutral">' in html
    assert "<strong>Live.</strong>" in html
    assert "Armed" not in html and "ssc-band" not in html

    rig.settings = replace(rig.settings, dry_run=True)
    rig.status = {**IMAGING, "dry_run": True}
    html = _get(rig, "/1/roof")
    assert '<p class="ssc-notice plan-mode" id="roof-mode" data-tone="attention">' in html
    assert "<strong>Dry run.</strong>" in html and "dry_run = false" in html
    assert "Live." not in html
    assert html.count("This is a dry run, so the scope will not move") == 2, "both sheets say so"


@pytest.mark.parametrize("status", [IMAGING, DAY_PARKED, {**DAY_PARKED, "connected": False, "parked": None}])
def test_normal_operation_has_no_danger_band(rig, status):
    rig.status = status
    assert "ssc-band" not in _get(rig, "/1/roof/status")


def test_a_closed_roof_with_the_scope_parked_is_not_danger(rig):
    rig.status = DAY_PARKED
    html = _get(rig, "/1/roof/status")
    assert 'data-tone="danger"' not in html.split('id="ssc-status"')[0]
    assert "Unsafe" in html


@pytest.mark.parametrize(
    "changes, words",
    [
        ({"loop_stale": True}, "Nothing will park the scope automatically"),
        ({"alarm": "Stacking at the wrong scale"}, "Stacking at the wrong scale"),
        ({"override_roof": True, "safe": False}, "Manual start is ignoring the roof"),
        ({"safe": False, "safety_detail": "read failed"}, "Roof unsafe, scope not parked"),
    ],
)
def test_active_danger_gets_the_band_in_the_polled_status(rig, changes, words):
    rig.status = {**IMAGING, **changes}
    html = _get(rig, "/1/roof/status")
    assert 'class="ssc-band"' in html and words in html
    assert html.index('class="ssc-band"') < html.index("plan-sentence")


def test_a_foreign_session_is_a_notice(rig):
    rig.status = {**IMAGING, "foreign": True}
    html = _get(rig, "/1/roof/status")
    assert "A session started elsewhere" in html and "ssc-band" not in html


def test_the_poll_updates_the_regions_below_the_controls_and_the_top_bar(rig):
    rig.status = {**IMAGING, "targets": [{"name": "M 42", "kind": "Single", "altitude": 40.0, "ok": True}]}
    polled = _get(rig, "/1/roof/status")
    for region in ("roof-targets", "roof-log", "ssc-status"):
        assert re.search(rf'id="{region}" hx-swap-oob="innerHTML"', polled), region
    assert "22:01 Start pressed" in polled and "Roof safe" in polled

    page = _get(rig, "/1/roof")
    assert "hx-swap-oob" not in page
    ids = _parse(page).ids
    assert len(ids) == len(set(ids)), "a page render has each region once"
    assert page.index("Session</h2>") < page.index('id="roof-log"')


def test_status_and_log_are_escaped(rig):
    rig.status = {**IMAGING, "log": ["<script>x</script>"], "alarm": "<b>a</b>", "target": "<i>t</i>"}
    for html in (_get(rig, "/1/roof"), _get(rig, "/1/roof/status")):
        assert "<script>x" not in html and "<b>a" not in html and "<i>t" not in html


def test_rejected_schedules_stay_listed_but_unselectable(rig, monkeypatch):
    rows = [{"name": "bad.json", "errors": ["no coordinates"], "warnings": [], "targets": []}]
    monkeypatch.setattr(roof_web, "_schedule_rows", lambda: rows)
    html = _get(rig, "/1/roof")
    assert re.search(r'<option value="bad.json" disabled>', html)
    assert "bad.json: no coordinates" in html


def test_the_auth_warning_survives_the_move_off_base(rig, monkeypatch):
    monkeypatch.setattr(front_app, "get_context", lambda *a: {**CONTEXT, "needs_auth_warning": True})
    for path in ("/1/roof", "/1/targets"):
        html = _get(rig, path)
        assert "Authentication required" in html and 'hx-get="/1/tonight/auth"' in html


def test_badge_is_a_compact_status_on_either_shell(rig):
    html = _get(rig, "/roof/badge")
    _no_bootstrap(html)
    assert 'class="ssc-status" data-tone="ok"' in html and "Roof safe" in html
    assert 'class="badge' not in html
    rig.status = {**IMAGING, "safe": False, "connected": False, "dry_run": True}
    html = _get(rig, "/roof/badge")
    assert all(word in html for word in ("Roof unsafe", "Scope offline", "Dry run"))
    assert "<svg" in html, "an icon, because Red has no hues"
    assert ".ssc-topbar__status" in html, "base.html does not load components.css"


# ---- Targets ---------------------------------------------------------------------------------


def _add(rig, name="M 42", **extra):
    body = {"name": name, "ra": "5.5881", "dec": "-5.39", "goal_hours": "6", "lp_filter": "on", **extra}
    assert rig.client.simulate_post("/1/targets/add", headers=FORM, body=urlencode(body)).status_code == 302
    return next(t for t in queue_web._queue().all() if t.name == name)


def test_targets_is_on_the_shell(rig):
    target = _add(rig)
    html = _get(rig, "/1/targets")
    assert "<title>Targets - SSC</title>" in html and ">Targets</h1>" in html
    _no_bootstrap(html)
    assert "<style" not in html and "confirm(" not in html
    assert 'id="target-sky-data"' in html and target.id in html


def test_targets_keeps_every_form(rig):
    target = _add(rig)
    html = _get(rig, "/1/targets")
    update = next(f for f in _parse(html).forms if f["id"] == f"target-{target.id}")
    assert update["action"] == "/1/targets/update" and update["method"] == "post"
    assert set(update["fields"]) == {
        "id", "enabled", "goal_hours", "min_session_hours", "prior_hours", "priority", "exposure_s", "lp_filter",
    }
    assert _form(html, "/1/targets/remove")["fields"]["id"]["value"] == target.id
    assert set(_form(html, "/1/targets/import")["fields"]) == {"source", "goal_hours"}
    added = [f for f in _parse(html).forms if f["action"] == "/1/targets/add"]
    assert [set(f["fields"]) for f in added if f["id"] is None] == [{"name", "ra", "dec", "goal_hours", "lp_filter"}]
    sky = next(f for f in added if f["id"] == "target-sky-add")
    assert set(sky["fields"]) == {"ra", "dec", "mosaic_scale", "mosaic_angle", "name", "goal_hours", "lp_filter"}
    assert set(_form(html, "/1/targets/framing")["fields"]) == {"id", "ra", "dec", "mosaic_scale", "mosaic_angle"}


def test_saving_a_row_untouched_changes_nothing(rig):
    target = _add(rig)
    queue_web._queue().update(target.id, priority="high", prior_hours=1.5, enabled=False, exposure_s=20.0, lp_filter=False)
    before = next(t for t in queue_web._queue().all() if t.id == target.id)
    html = _get(rig, "/1/targets")
    block = html[html.index(f'id="target-{target.id}"'):html.index("</form>", html.index(f'id="target-{target.id}"'))]
    fields = {"id": target.id}
    for name in ("goal_hours", "min_session_hours", "prior_hours"):
        fields[name] = re.search(rf'name="{name}" type="text" value="([^"]*)"', block).group(1)
    fields["priority"] = re.search(r'name="priority" value="(\w+)" checked', block).group(1)
    fields["exposure_s"] = re.search(r'name="exposure_s" type="number" value="([^"]*)"', block).group(1)
    fields["lp_filter"] = re.search(r'name="lp_filter" value="(\w+)" checked', block).group(1)
    assert 'name="enabled" type="checkbox" role="switch" checked' not in block
    assert rig.client.simulate_post("/1/targets/update", headers=FORM, body=urlencode(fields)).status_code == 302
    assert next(t for t in queue_web._queue().all() if t.id == target.id) == before


def test_remove_needs_its_sheet(rig):
    target = _add(rig, name='<img src=x>"')
    html = _get(rig, "/1/targets")
    main, sheets = html.split("</main>")
    assert f'data-open-sheet="target-remove-{target.id}"' in main
    assert "/targets/remove" not in main
    assert f'id="target-remove-{target.id}"' in sheets
    assert "Its recorded sessions are kept." in sheets
    assert "<img src=x>" not in html, "names are escaped"


def test_a_row_is_name_bar_and_forecast(rig):
    _add(rig)
    html = _get(rig, "/1/targets")
    start = html.index('<summary class="plan-target__summary"')
    summary = html[start:html.index("</summary>", start)]
    assert "M 42" in summary and 'role="progressbar"' in summary and "plan-target__forecast" in summary
    assert "<input" not in summary


def test_sky_view_has_every_id_its_script_uses(rig):
    html = _get(rig, "/1/targets")
    script = (Path(__file__).parent.parent / "front/public/target_sky.js").read_text()
    wanted = set(re.findall(r'(?:field|getElementById)\("([^"]+)"\)', script))
    wanted |= {f"{prefix}-{part}" for prefix in ("target-sky-add", "target-sky-save") for part in ("ra", "dec", "scale", "angle")}
    ids = set(_parse(html).ids)
    assert wanted and wanted <= ids, wanted - ids
    for src in ("seestar_models.js", "aladin_loader.js", "frame_geometry.js", "target_sky.js"):
        assert f"/public/{src}" in html


def test_sky_objects_have_every_id_their_script_uses(rig):
    html = _get(rig, "/1/targets")
    script = (Path(__file__).parent.parent / "front/public/sky_objects.js").read_text()
    wanted = set(re.findall(r'field\("([^"]+)"\)', script))
    assert wanted and wanted <= set(_parse(html).ids), wanted - set(_parse(html).ids)
    assert "/public/sky_objects.js" in html
    assert html.index("/public/sky_objects.js") < html.index("/public/target_sky.js")
    assert 'data-sky-catalog="/public/ssc/sky_catalog.json"' in html


def test_sky_objects_stay_hidden_until_they_can_work(rig):
    # Without Aladin or the catalog, the page shows what it showed before.
    html = _get(rig, "/1/targets")
    for element in ("target-sky-objects", "target-sky-pick"):
        tag = re.search(rf'<div id="{element}"[^>]*>', html).group(0)
        assert " hidden" in tag, element
    script = (Path(__file__).parent.parent / "front/public/target_sky.js").read_text()
    assert "if (window.SkyObjects)" in script


def test_sky_objects_add_no_form_or_route(rig):
    html = _get(rig, "/1/targets")
    pick = html[html.index('id="target-sky-pick"'): html.index('id="target-sky-frame-controls"')]
    assert "<form" not in pick
    assert 'type="button"' in pick
    script = (Path(__file__).parent.parent / "front/public/sky_objects.js").read_text()
    assert "POST" not in script and "/targets" not in script
    page = (Path(__file__).parent.parent / "front/public/target_sky.js").read_text()
    assert "onFrame: (entry) => startNew(entry.r, entry.d, catalogName(entry))" in page
    assert "onShow: select" in page


def test_sky_objects_go_under_the_owners_labels(rig):
    # Aladin draws catalogs in the order added, so the owner's must be added last.
    page = (Path(__file__).parent.parent / "front/public/target_sky.js").read_text()
    assert page.index("window.SkyObjects.start(") < page.index("    labels();\n")


def test_red_colours_keep_their_red_channel():
    # Red's floor keeps only the red channel, so a canvas colour needs a strong one.
    public = Path(__file__).parent.parent / "front/public"
    page = (public / "target_sky.js").read_text()
    red = re.search(r"red: \{([^}]*)\}", page).group(1)
    sky = re.search(r'MARKER = \{[^}]*red: "#(\w{6})"', (public / "sky_objects.js").read_text()).group(1)
    for value in [*re.findall(r'"#(\w{6})"', red), sky]:
        assert int(value[:2], 16) >= 0xC0, value


def test_show_buttons_are_wired_for_the_sky_script(rig):
    target = _add(rig)
    assert f'data-sky-show="{target.id}"' in _get(rig, "/1/targets")


def test_empty_list_and_unknown_site_say_so(rig):
    assert "Nothing listed yet." in _get(rig, "/1/targets")
    rig.settings = replace(rig.settings, lat=0, lon=0)
    assert "Set the site latitude and longitude before planning." in _get(rig, "/1/targets")


def test_top_bar_status_keeps_refreshing_on_targets(rig):
    html = _get(rig, "/1/targets")
    assert 'hx-get="/roof/badge"' in html and 'hx-target="#ssc-status"' in html
    assert "Roof safe" in html


@pytest.mark.parametrize("path", ["/1/roof", "/1/targets"])
def test_a_proxy_prefix_reaches_every_action(rig, path):
    _add(rig)
    html = _get(rig, path, headers={"X-Forwarded-Prefix": "/ssc"})
    forms = _parse(html).forms
    assert forms and all(f["action"].startswith("/ssc/1/") for f in forms)
    assert 'hx-get="/ssc/1/tonight/auth"' in html


def test_plan_css_is_loaded_and_hidden_beats_its_layout_classes():
    public = Path(__file__).parent.parent / "front/public/ssc"
    assert '@import url("plan.css") layer(pages);' in (public / "ssc.css").read_text()
    # target_sky.js shows the add or the save form by toggling hidden on .plan-stack elements.
    assert ".plan-page [hidden] { display: none; }" in (public / "plan.css").read_text()


def test_held_outranks_a_waiting_headline_so_it_matches_tonight(rig):
    rig.status = {**IMAGING, "held": True, "headline": "waiting: roof must read safe for 120s"}
    html = _get(rig, "/1/roof")
    assert "Held until Start or the next night" in html
    assert "roof must read safe" not in html.split("plan-sentence", 1)[1][:80]


# ---- Discord alerts on Automation ------------------------------------------------------------

HOOK = "https://discord.com/api/webhooks/1568293035819802855/s3cret-token"


@pytest.fixture
def alerts_rig(rig, monkeypatch, tmp_path):
    from roof import alerts as roof_alerts

    path = tmp_path / "hook.txt"
    rig.saved = {}
    rig.tests = []
    rig.hook_path = path
    rig.alerts = roof_alerts
    monkeypatch.setattr(roof_alerts, "webhook_path", lambda: str(path))
    monkeypatch.setattr(roof_alerts, "_last_delivery", None)
    monkeypatch.setattr(roof_web.roof_config, "save", rig.saved.__setitem__)

    def send_test(url):
        rig.tests.append(url)
        return True, "HTTP 204"

    monkeypatch.setattr(roof_alerts, "send_test", send_test)
    return rig


def _post_alert(rig, route, body=""):
    return rig.client.simulate_post(f"/1/roof/alerts/{route}", headers=FORM, body=body)


def test_alerts_panel_without_a_webhook_asks_for_one(alerts_rig):
    html = _get(alerts_rig, "/1/roof")
    assert "Nothing posts until a webhook is saved" in html
    assert 'name="webhook"' in html and 'type="password"' in html
    assert "required" in _form(html, "/1/roof/alerts/webhook")["fields"]["webhook"]
    assert 'action="/1/roof/alerts/test"' not in html, "nothing to test yet"
    assert "Turn alerts on" not in html, "can't be turned on yet"


def test_saving_a_webhook_never_sends_it_back_to_the_browser(alerts_rig):
    result = _post_alert(alerts_rig, "webhook", urlencode({"webhook": HOOK}))
    assert result.status_code == 302
    assert alerts_rig.alerts.read_webhook(str(alerts_rig.hook_path)) == HOOK
    assert "s3cret-token" not in str(result.headers)
    html = _get(alerts_rig, "/1/roof")
    assert "s3cret-token" not in html and "1568293035819802855" not in html
    assert "ID ending 2855" in html
    assert _form(html, "/1/roof/alerts/test")["fields"] == {}
    assert "Turn alerts on" in html


def test_a_bad_webhook_is_refused_and_keeps_the_saved_one(alerts_rig):
    alerts_rig.alerts.write_webhook(str(alerts_rig.hook_path), HOOK)
    _post_alert(alerts_rig, "webhook", urlencode({"webhook": "https://evil.example/api/webhooks/1/a"}))
    assert alerts_rig.alerts.read_webhook(str(alerts_rig.hook_path)) == HOOK


def test_alerts_cannot_be_turned_on_without_a_webhook(alerts_rig):
    _post_alert(alerts_rig, "enable", "enabled=true")
    assert alerts_rig.saved == {}


def test_alerts_turn_on_and_off(alerts_rig):
    alerts_rig.alerts.write_webhook(str(alerts_rig.hook_path), HOOK)
    _post_alert(alerts_rig, "enable", "enabled=true")
    assert alerts_rig.saved == {"discord_enabled": True}
    alerts_rig.settings = replace(alerts_rig.settings, discord_enabled=True)
    html = _get(alerts_rig, "/1/roof")
    assert "Alerts are on" in html and "Turn alerts off" in html
    assert _form(html, "/1/roof/alerts/enable")["fields"]["enabled"]["value"] == "false"
    _post_alert(alerts_rig, "enable", "enabled=false")
    assert alerts_rig.saved == {"discord_enabled": False}


def test_alert_switches_show_and_save_what_is_muted(alerts_rig):
    alerts_rig.settings = replace(alerts_rig.settings, discord_muted=("autofocus_passed",))
    html = _get(alerts_rig, "/1/roof")
    assert re.search(r'name="event_autofocus_failed"[^>]* checked', html)
    assert not re.search(r'name="event_autofocus_passed"[^>]* checked', html)
    assert "Alarms and lost contact with the scope always post" in html
    kinds = set(alerts_rig.alerts.KINDS) - {"stacking", "parked"}
    _post_alert(alerts_rig, "events", "&".join(f"event_{k}=on" for k in kinds))
    assert set(alerts_rig.saved["discord_muted"]) == {"stacking", "parked"}


def test_alert_test_button_posts_only_to_the_saved_webhook(alerts_rig):
    _post_alert(alerts_rig, "test")
    assert alerts_rig.tests == []
    alerts_rig.alerts.write_webhook(str(alerts_rig.hook_path), HOOK)
    _post_alert(alerts_rig, "test", urlencode({"webhook": "https://evil.example/x"}))
    assert alerts_rig.tests == [HOOK]


def test_webhook_can_be_removed(alerts_rig):
    alerts_rig.alerts.write_webhook(str(alerts_rig.hook_path), HOOK)
    _post_alert(alerts_rig, "webhook/remove")
    assert not alerts_rig.hook_path.exists()
    assert "Nothing posts until a webhook is saved" in _get(alerts_rig, "/1/roof")


def test_a_failed_post_shows_on_the_page(alerts_rig, monkeypatch):
    monkeypatch.setattr(
        alerts_rig.alerts,
        "_last_delivery",
        {"title": "Arm parked", "sent": False, "status": "HTTP 404", "at": "07:12"},
    )
    html = _get(alerts_rig, "/1/roof")
    assert "Last alert didn't post at 07:12: Arm parked (HTTP 404)" in html


@pytest.mark.parametrize("route", ["enable", "webhook", "webhook/remove", "test", "events"])
def test_alert_routes_refuse_other_sites(alerts_rig, route):
    alerts_rig.alerts.write_webhook(str(alerts_rig.hook_path), HOOK)
    result = alerts_rig.client.simulate_post(
        f"/1/roof/alerts/{route}",
        headers={**FORM, "Origin": "http://evil.example", "Host": "localhost:5432"},
        body="enabled=false",
    )
    assert result.status_code == 403
    assert alerts_rig.hook_path.exists() and not alerts_rig.saved and not alerts_rig.tests


def test_targets_page_has_the_reach_controls(rig):
    html = _get(rig, "/1/targets")
    assert 'data-sky-window="/1/targets/sky.json"' in html
    assert 'name="target-sky-reach-days"' in html and 'id="target-sky-reach-on"' in html
    assert html.index("public/sky_reach.js") < html.index("public/target_sky.js")
    for element in ("target-sky-reach-note", "target-sky-pick-reach", "target-sky-frame-reach"):
        assert f'id="{element}"' in html
