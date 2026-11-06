"""The Settings screen on the shell: every field keeps its name and value."""

import re

import pytest

import front.app as front_app

TELESCOPES = [{"device_num": 1, "name": "Alpha", "ip_address": "10.0.0.1"}]
CONTEXT = {
    "root": "/1", "root_path": "", "telescope": TELESCOPES[0], "telescopes": TELESCOPES,
    "experimental": True, "platform": "raspberry_pi", "partial_path": "settings",
    "online": True, "client_master": True, "webui_theme": "dark", "version": "test",
}
SETTINGS = {
    "stack_dither_pix": 50, "stack_dither_interval": 10, "stack_dither_enable": True,
    "exp_ms_stack_l": 10000, "exp_ms_continuous": 500, "heater_enable": False,
    "auto_power_off": True, "stack_lenhance": False, "dark_mode": False,
    "temp_unit": "C", "light_duration_min": -1, "focal_pos": 1500, "wide_cam": True,
    "stack_capt_type": "light",
}


def _render(monkeypatch, settings=SETTINGS, template="settings.html", **extra):
    captured = {}
    monkeypatch.setattr(front_app, "get_context", lambda *a: dict(CONTEXT, **extra))
    monkeypatch.setattr(front_app, "get_firmware_ver_int", lambda *a: 3000)
    monkeypatch.setattr(front_app, "get_device_settings", lambda *a: dict(settings))
    monkeypatch.setattr(front_app, "check_api_state", lambda *a: True)
    monkeypatch.setattr(front_app, "render_template", lambda req, resp, name, **c: captured.update(c, _name=name))
    front_app.SettingsResource().render_settings(None, None, 1, "Saved.")
    assert captured.pop("_name") == "settings.html"
    captured.update(action="/1/settings", flashed_messages=[], messages=[])
    return front_app.fetch_template(template).render(**captured)


def _fields(html):
    form = html[html.index('<form id="settingsForm"'):html.index("</form>")]
    pairs = re.findall(r'<input[^>]*?name="([^"]+)"[^>]*?value="([^"]*)"', form)
    return {(n, v) for n, v in pairs}


def test_every_field_keeps_its_name_and_value(monkeypatch):
    html = _render(monkeypatch)
    fields = _fields(html)
    names = {n for n, _ in fields}
    assert names == set(SETTINGS)
    assert ("stack_dither_enable", "True") in fields and ("stack_dither_enable", "false") in fields
    assert ("heater_enable", "true") in fields and ("heater_enable", "False") in fields
    assert ("exp_ms_stack_l", "10000") in fields and ("temp_unit", "C") in fields
    assert re.search(r'id="heater_enable_disable" name="heater_enable" value="False" checked', html)
    assert re.search(r'id="stack_dither_enable_enable" name="stack_dither_enable" value="True" checked', html)


def test_validation_attributes_survive(monkeypatch):
    html = _render(monkeypatch)
    assert re.search(r'id="exp_ms_stack_l" name="exp_ms_stack_l" type="number" value="10000" min="5" max="90000" required', html)
    assert re.search(r'id="stack_dither_pix"[^>]*min="10" max="200" required', html)
    assert 'pattern="[Cc]|[Ff]"' in html


def test_form_posts_to_the_same_action_and_groups_stay_in_order(monkeypatch):
    html = _render(monkeypatch)
    assert '<form id="settingsForm" class="s-form" method="post" action="/1/settings">' in html
    titles = re.findall(r'class="ssc-form-group__title">([^<]+)<', html)
    assert titles == ["Appearance", "Imaging", "Environment", "General"]
    for element in ("saveSettingsBtn", "resetSettingsBtn", "unsavedPill"):
        assert f'id="{element}"' in html


def test_firmware_note_for_default_light_duration(monkeypatch):
    assert "using the device default (-1)" in _render(monkeypatch)


def test_shell_page_loads_no_bootstrap(monkeypatch):
    html = _render(monkeypatch)
    assert "bootstrap" not in html.lower()
    assert not re.search(r'class="[^"]*\b(form-control|btn|col-md|d-flex|form-check)', html)


def test_appearance_group_sits_outside_the_form(monkeypatch):
    html = _render(monkeypatch)
    assert html.index("Appearance") < html.index('<form id="settingsForm"')


@pytest.mark.parametrize("extra,text", [({"client_master": False}, "guest mode"), ({"online": False}, "offline mode")])
def test_guest_and_offline_states_have_no_form(monkeypatch, extra, text):
    html = _render(monkeypatch, settings={} if "online" in extra else SETTINGS, **extra)
    assert text in html and '<form id="settingsForm"' not in html
    assert "Appearance" in html
