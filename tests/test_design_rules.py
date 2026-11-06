"""Keeps the design system from drifting. Rules: front/DESIGN.md."""

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent / "front"
SSC = ROOT / "public" / "ssc"
# tokens.css defines the values. red-floor.css is the mechanism behind Red, not a choice.
EXEMPT = {"tokens.css", "red-floor.css"}

# Inline styles per legacy template. Lower these as pages move. They may never rise.
INLINE_STYLE_BASELINE = {
    "do_command.html": 8,
    "framed_mosaic_create.html": 5,
    "goto_target.html": 3,
    "image_create.html": 3,
    "live.html": 1,
    "live_star.html": 1,
    "mosaic_create.html": 3,
    "pa_refine.html": 6,
    "partials/astro_mosaic.html": 8,
    "partials/astrospheric.html": 1,
    "partials/bubble_level.html": 2,
    "partials/dashboard_device.html": 1,
    "partials/dashboard_focus.html": 4,
    "partials/dec_offset.html": 3,
    "partials/events.html": 1,
    "partials/framed_mosaic_planning.html": 2,
    "partials/guestmode_content.html": 2,
    "partials/home_content.html": 10,
    "partials/live_card_status.html": 1,
    "partials/live_movement_control.html": 2,
    "partials/mosaic_order_diagram.html": 1,
    "partials/roof_status.html": 1,
    "partials/stats_content.html": 4,
    "partials/target_sky.html": 2,
    "platform_rpi.html": 4,
    "target_list.html": 5,
}


def _stylesheets():
    return [p for p in SSC.glob("*.css") if p.name not in EXEMPT]


def _strip_comments(css):
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def test_no_raw_colours_outside_the_tokens():
    for sheet in _stylesheets():
        css = _strip_comments(sheet.read_text())
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(", css), sheet.name


def test_type_and_shape_come_from_tokens():
    for sheet in _stylesheets():
        css = _strip_comments(sheet.read_text())
        for prop in ("font-size", "font-weight", "border-radius"):
            for value in re.findall(rf"{prop}\s*:\s*([^;]+);", css):
                assert value.strip().startswith("var(--ssc-"), f"{sheet.name}: {prop}: {value}"


def test_no_important_under_ssc():
    for sheet in _stylesheets():
        assert "!important" not in _strip_comments(sheet.read_text()), sheet.name


def test_tokens_define_every_theme():
    tokens = (SSC / "tokens.css").read_text()
    for theme in ("night", "day", "red"):
        assert f'[data-theme="{theme}"]' in tokens


def test_legacy_inline_styles_only_fall():
    for template in (ROOT / "templates").rglob("*.html"):
        name = str(template.relative_to(ROOT / "templates"))
        count = template.read_text().count('style="')
        assert count <= INLINE_STYLE_BASELINE.get(name, 0), f"{name} has {count} inline styles"
