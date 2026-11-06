"""Keeps the design system from drifting. Rules: front/DESIGN.md."""

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent / "front"
SSC = ROOT / "public" / "ssc"
SSC_TEMPLATES = ROOT / "templates" / "ssc"
# tokens.css defines the values. red-floor.css is the mechanism behind Red, not a choice.
EXEMPT = {"tokens.css", "red-floor.css"}

# Inline styles per legacy template. Lower these as pages move. They may never rise.
INLINE_STYLE_BASELINE = {
    "partials/astro_mosaic.html": 8,
    "partials/dashboard_device.html": 1,
    "partials/dashboard_focus.html": 4,
    "partials/events.html": 1,
    "partials/framed_mosaic_planning.html": 1,
    "partials/home_content.html": 4,
    "partials/mosaic_order_diagram.html": 1,
    "partials/stats_content.html": 4,
    "ssc/shell.html": 1,
    "ssc/ui.html": 5,
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
        if name.startswith("ssc/"):
            continue  # stricter rules below
        count = template.read_text().count('style="')
        assert count <= INLINE_STYLE_BASELINE.get(name, 0), f"{name} has {count} inline styles"


def _templates():
    templates = list(SSC_TEMPLATES.rglob("*.html"))
    assert templates
    return templates


def _strip_jinja(html):
    html = re.sub(r"\{#.*?#\}", "", html, flags=re.S)
    return re.sub(r"\{%.*?%\}", "", html, flags=re.S)


def test_spacing_comes_from_tokens():
    pattern = re.compile(r"(?<![-\w])(padding|margin|gap|column-gap|row-gap)(-[a-z]+)?\s*:\s*([^;]+);")
    for sheet in _stylesheets():
        for match in pattern.finditer(_strip_comments(sheet.read_text())):
            for part in re.sub(r"(var|calc|env)\([^;]*\)", "", match.group(3)).split():
                assert part in ("0", "auto"), f"{sheet.name}: {match.group(0)}"


def test_ssc_templates_have_no_style_blocks_or_raw_colours():
    for template in _templates():
        html = _strip_jinja(template.read_text())
        assert "<style" not in html, template.name
        assert "!important" not in html, template.name
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(", html), template.name


def test_ssc_inline_styles_only_set_css_variables():
    for template in _templates():
        for value in re.findall(r'style="([^"]*)"', _strip_jinja(template.read_text())):
            for declaration in filter(None, (d.strip() for d in value.split(";"))):
                assert declaration.startswith("--"), f"{template.name}: {declaration}"


BOOTSTRAP = re.compile(
    r"\b(btn|btn-[a-z-]+|card|row|col|col-\S+|container|navbar\S*|nav-link|nav-item|dropdown\S*|modal\S*|"
    r"badge|alert|alert-\S+|form-\S+|d-\S+|mb-\d|mt-\d|text-bg-\S+|text-danger|text-warning|text-success|table)\b"
)


def test_ssc_templates_use_no_bootstrap_classes_or_behaviour():
    for template in _templates():
        html = _strip_jinja(template.read_text())
        for classes in re.findall(r'class="([^"]*)"', html):
            match = BOOTSTRAP.search(re.sub(r"\{\{.*?\}\}", "", classes))
            assert not (match and match.group(0) in classes.split()), f"{template.name}: {classes}"
        assert "bootstrap" not in html.replace("without Bootstrap", "").lower(), template.name
        assert "data-bs-toggle" not in html, template.name


def test_no_native_confirm_under_ssc():
    files = _templates() + list(SSC.glob("*.js"))
    for path in files:
        assert not re.search(r"\bconfirm\(", path.read_text()), path.name


def test_no_looping_animation_under_ssc():
    for sheet in SSC.glob("*.css"):
        assert "infinite" not in sheet.read_text(), sheet.name
