"""Live page: the ids and classes the scripts bind to must survive the restyle."""

import re
from pathlib import Path

import jinja2
import pytest

import front.app as front_app
from tests.test_front_app_state import _minimal_context

FRONT = Path(__file__).parent.parent / "front"
LIVE_CSS = FRONT / "public" / "ssc" / "live.css"

# Every id liveview.js, live_base.html's script or the htmx hooks look up.
COMMON_IDS = ["liveViewImg", "liveRotateButton"]
MODE_PAGES = {
    "live_star.html": ["movement-controls", "zone", "focus-controls", "current-focus-value",
                       "focus-extra-backward", "focus-backward", "focus-forward", "focus-extra-forward",
                       "open_stop_exposure_dialog_btn2"],
    "live_sun.html": ["movement-controls", "zone", "zoom1x", "zoom2x", "zoom4x", "recordButtonContent",
                      "timelapse", "rawCheckbox", "raw", "capture_status", "open_stop_exposure_dialog_btn2"],
    "live_moon.html": ["movement-controls", "zone", "zoom1x", "focus-controls", "exposure-controls",
                       "exposure-slider", "gain-range", "current-exposure-value", "current-gain-value",
                       "wide-cam-control", "capture_status"],
    "live_planet.html": ["movement-controls", "zone", "open_stop_exposure_dialog_btn2"],
    "live_scenery.html": ["movement-controls", "zone", "focus-controls", "exposure-controls"],
    "live_solarsys.html": ["movement-controls", "zone", "zoom2x", "focus-controls", "exposure-controls",
                           "capture_status", "target_name"],
    "live.html": ["moon", "planet", "scenery", "star", "sun", "open_stop_exposure_dialog_btn2"],
}


def _render(name):
    env = front_app.env
    old = env.undefined
    env.undefined = jinja2.ChainableUndefined
    try:
        ctx = _minimal_context("live")
        ctx.update(flashed_messages=[], messages=[], version="t", now="22:14", root_path="",
                   view={"target_name": "Jupiter"}, wide_cam=False)
        return front_app.fetch_template(name).render(**ctx)
    finally:
        env.undefined = old


@pytest.mark.parametrize("name,ids", MODE_PAGES.items())
def test_scripted_ids_survive(name, ids):
    html = _render(name)
    for ident in ids + (COMMON_IDS if name != "live.html" else []):
        assert f'id="{ident}"' in html or f"id='{ident}'" in html, f"{name}: {ident}"


@pytest.mark.parametrize("name", [n for n in MODE_PAGES if n != "live.html"])
def test_each_scripted_control_id_is_unique(name):
    html = _render(name)
    for ident in ("movement-controls", "focus-controls", "exposure-controls", "liveViewImg", "zone"):
        assert len(re.findall(rf'id="{ident}"', html)) <= 1, f"{name}: duplicate {ident}"


def test_mode_buttons_keep_the_class_liveview_js_toggles():
    html = _render("live.html")
    assert html.count("mode-button") >= 6
    assert "btn-secondary" in html


def test_live_base_loads_live_css_after_design_css():
    html = _render("live_star.html")
    assert html.index("public/design.css") < html.index("public/ssc/live.css")


def test_focus_steps_and_hooks_keep_their_values():
    html = _render("live_star.html")
    for inc in ("-50", "-10", "10", "50"):
        assert f'hx-vals=\'{{"inc": {inc}}}\'' in html
    assert 'sse-swap="focusMove"' in html
    assert "start_auto_focus" in html


def test_live_css_has_no_important_and_sets_touch_targets():
    css = re.sub(r"/\*.*?\*/", "", LIVE_CSS.read_text(), flags=re.S)
    assert "!important" not in css
    assert "340px" in css
    assert css.count("var(--ssc-touch)") >= 5


def test_live_templates_use_no_important_utility_classes_on_controls():
    # Bootstrap utilities such as m-1 and p-3 carry !important and cannot be overridden.
    for name in ("live_star.html", "live_moon.html", "live.html"):
        html = _render(name)
        for classes in re.findall(r'class="([^"]*)"', html):
            parts = classes.split()
            assert not {"m-1", "p-3", "mb-3"} & set(parts), f"{name}: {classes}"
