"""The four destinations: section highlighting, reachability, short URLs and the theme control."""

import ast
import re
from pathlib import Path
from types import SimpleNamespace

import falcon
import falcon.testing
import pytest

import front.app as front_app
from roof import config as roof_config, web as roof_web

TELESCOPES = [
    {"device_num": 1, "name": "Alpha", "ip_address": "10.0.0.1"},
    {"device_num": 2, "name": "Beta", "ip_address": "10.0.0.2"},
]
PUBLIC = Path(front_app.__file__).with_name("public")
TEMPLATES = Path(front_app.__file__).with_name("templates")


def _render(template, partial_path="", **extra):
    context = {
        "root": "/1", "root_path": "", "telescope": TELESCOPES[0], "telescopes": TELESCOPES,
        "experimental": True, "platform": "raspberry_pi", "partial_path": partial_path, **extra,
    }
    return front_app.fetch_template(template).render(**context)


def _current_section(html):
    return re.findall(r'class="ssc-tab" href="[^"]*" aria-current="true">.*?</svg>(\w+)</a>', html, flags=re.S)


def _current_page(html):
    return re.findall(r'class="ssc-subnav__item" href="[^"]*" aria-current="page">([^<]+)</a>', html)


def _subnav(html):
    return re.findall(r'class="ssc-subnav__item" href="([^"]*)"', html)


@pytest.mark.parametrize(
    "path,section,page",
    [
        ("dashboard", "Tonight", None),
        ("targets", "Plan", "Targets"),
        ("roof", "Plan", "Automation"),
        ("schedule", "Plan", "Sequences"),
        ("schedule/image", "Plan", "Sequences"),
        ("schedule/wait-until", "Plan", "Sequences"),
        ("framed_mosaic", "Plan", "Frame"),
        ("planning", "Plan", "Planning"),
        ("mosaic", "Plan", "Mosaic"),
        ("live", "Scope", "Live"),
        ("live/star", "Scope", "Live"),
        ("goto", "Scope", "Goto"),
        ("image", "Scope", "Image"),
        ("startup", "Scope", "Prepare"),
        ("pa_refine", "Scope", "Prepare"),
        ("", "Scope", "Device"),
        ("settings", "System", "Settings"),
        ("config", "System", "Config"),
        ("command", "System", "Commands"),
        ("stats", "System", "Stats"),
        ("guestmode", "System", "Guest mode"),
        ("support", "System", "Support"),
        ("platform-rpi", "System", "Platform"),
        ("system", "System", "System"),
    ],
)
def test_the_section_and_page_follow_the_path_and_its_sub_paths(path, section, page):
    html = _render("ssc/nav.html", path)
    assert _current_section(html) == [section]
    assert _current_page(html) == ([page] if page else [])


def test_a_path_the_nav_does_not_know_highlights_nothing_and_still_renders():
    html = _render("ssc/nav.html", "eventstatus")
    assert _current_section(html) == [] and _subnav(html) == []


def test_top_bar_and_tab_bar_are_exactly_the_four_destinations():
    html = _render("ssc/nav.html", "live")
    assert re.findall(r'class="ssc-widenav__item" href="[^"]*"[^>]*>([^<]+)</a>', html) == ["Tonight", "Plan", "Scope", "System"]
    assert re.findall(r'</svg>(\w+)</a>', html) == ["Tonight", "Plan", "Scope", "System"]
    assert "More" not in html
    assert len(re.findall(r'class="ssc-tab"', html)) == 4
    assert all(size == "24" for size in re.findall(r'class="ssc-tab"[^>]*><svg class="ssc-icon" aria-hidden="true" width="(\d+)"', html))


def test_conditional_pages_leave_the_sub_navigation():
    html = _render("ssc/nav.html", "targets", experimental=False)
    assert "/1/planning" not in html
    html = _render("ssc/nav.html", "settings", platform="darwin")
    assert "/1/platform-rpi" not in html


def test_polar_align_points_at_the_panel_on_prepare():
    assert 'href="/1/startup#paRefineContent"' in _render("ssc/nav.html", "startup")
    assert 'id="paRefineContent"' in (TEMPLATES / "startup.html").read_text()


def _registered_routes():
    """Every path the app registers, read from the source so nothing starts."""
    tree = ast.parse(Path(front_app.__file__).read_text())
    routes = [
        call.args[0].value
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and getattr(call.func, "attr", "") == "add_route"
        and isinstance(call.args[0], ast.Constant)
    ]
    return routes


def _app_with_every_route(monkeypatch):
    automation = SimpleNamespace(status=dict, start=lambda: None)
    monkeypatch.setattr(roof_web, "get_automation", lambda: automation)
    monkeypatch.setattr(roof_web.focus, "listen", lambda: None)
    app = falcon.App(middleware=[front_app.RootPathMiddleware()])
    roof_web.install(app)
    for route in _registered_routes():
        app.add_route(route, _Page())
    front_app.install_section_redirects(app)
    return app


class _Page:
    def on_get(self, req, resp, **_):
        resp.text = "page"


def test_every_nav_link_on_every_page_resolves_to_a_route(monkeypatch):
    app = _app_with_every_route(monkeypatch)
    seen = set()
    for path in ("dashboard", "targets", "live", "settings", "live/star", "schedule/image"):
        for href in re.findall(r'href="(/[^"#]*)', _render("ssc/nav.html", path)):
            seen.add(href)
            assert app._router.find(href) is not None, href
    # Everything the page table names is among them.
    for route in ("dashboard", "targets", "roof", "schedule", "framed_mosaic", "planning", "mosaic", "live",
                  "goto", "image", "startup", "", "settings", "config", "command", "stats", "guestmode",
                  "support", "platform-rpi", "system"):
        assert f"/1/{route}" in seen, route


def test_no_page_route_loses_its_way_into_the_nav():
    """A new GET page must be added to nav_links.html, or to this list with a reason."""
    html = "".join(_render("ssc/nav.html", p) for p in ("dashboard", "targets", "live", "settings"))
    reachable = {href.split("#")[0].removeprefix("/1") for href in re.findall(r'href="(/[^"]*)"', html)}
    not_pages = {
        "/console",  # a route with no template, so it only returns an error
        "/pa_refine",  # a bare fragment, shown on Prepare
        "/position", "/search", "/eventstatus", "/reload", "/config.json", "/simbad", "/stellarium",
        "/toggleuitheme", "/toggleplanningcard", "/collapseplanningcard", "/updatetwilighttimes",
        "/getbalancesensor", "/gensupportbundle", "/getplanetcoordinates", "/getcometcoordinates",
        "/localsearch", "/getminorplanetcoordinates", "/getaavsocoordinates", "/home-content",
        "/stats-content", "/guestmode-content", "/auth-status", "/plan", "/scope", "/api", "/imaging",
    }
    for route in _registered_routes():
        path = route.replace("/{telescope_id:int}", "") or "/"
        top = "/" + path.lstrip("/").split("/")[0]
        if path.startswith("/public") or top in not_pages:
            continue
        assert top in reachable, route


@pytest.mark.parametrize(
    "url,target",
    [
        ("/1/plan", "/1/targets"),
        ("/1/scope", "/1/live"),
        ("/0/plan", "/0/targets"),
        ("/0/scope", "/0/live"),
        ("/plan", "/targets"),
        ("/scope", "/live"),
        ("/system", "/settings"),
    ],
)
def test_short_section_urls_redirect_to_the_first_page(url, target):
    app = falcon.App(middleware=[front_app.RootPathMiddleware()])
    front_app.install_section_redirects(app)
    response = falcon.testing.TestClient(app).simulate_get(url)
    assert response.status_code == 302 and response.headers["location"] == target
    prefixed = falcon.testing.TestClient(app).simulate_get(url, headers={"X-Forwarded-Prefix": "/p"})
    assert prefixed.headers["location"] == "/p" + target


def test_the_system_page_keeps_its_url_when_the_redirects_are_installed(monkeypatch):
    app = _app_with_every_route(monkeypatch)
    assert isinstance(app._router.find("/1/system")[0], _Page)
    assert isinstance(app._router.find("/1/plan")[0], front_app.SectionRedirect)


def test_with_roof_automation_short_urls_go_through_the_configured_device(monkeypatch):
    monkeypatch.setattr(roof_config, "load", lambda: SimpleNamespace(device_num=1))
    client = falcon.testing.TestClient(_app_with_every_route(monkeypatch))
    assert client.simulate_get("/plan").headers["location"] == "/1/plan"
    assert client.simulate_get("/1/plan").headers["location"] == "/1/targets"
    assert client.simulate_get("/scope").headers["location"] == "/1/scope"
    # DefaultDeviceMiddleware finds the existing page first, so this one stays the System page.
    assert client.simulate_get("/system").headers["location"] == "/1/system"
    assert client.simulate_get("/1/system").status_code == 200


def test_scope_switching_keeps_the_page_and_sub_path():
    html = _render("ssc/nav.html", "schedule/image")
    menu = html[html.index('class="ssc-menu ssc-scope"'):html.index("</details>")]
    for href in ("/1/schedule/image", "/2/schedule/image", "/0/schedule/image"):
        assert f'href="{href}"' in menu
    menu = _render("ssc/nav.html", "")
    assert 'href="/0/"' in menu and 'href="/2/"' in menu


def test_root_path_prefixes_every_link():
    html = _render("ssc/nav.html", "live", root="/p/1", root_path="/p")
    hrefs = re.findall(r'href="([^"]*)"', html)
    assert hrefs and all(href.startswith("/p/") and not href.startswith("/p/p/") for href in hrefs)
    assert 'hx-get="/p/roof/badge"' in html


def test_status_is_polled_except_where_the_page_renders_it():
    assert 'hx-get="/roof/badge"' in _render("ssc/nav.html", "live")
    html = _render("ssc/nav.html", "dashboard", roof_lines=[{"tone": "ok", "text": "Roof safe"}])
    assert "hx-get" not in html and "Roof safe" in html


class _Req:
    def __init__(self, uri):
        self.relative_uri = uri


def test_a_poll_filling_the_context_cache_cannot_change_the_next_pages_nav(monkeypatch):
    monkeypatch.setattr(front_app, "_context_cached", {})
    monkeypatch.setattr(front_app, "_last_context_get_time", {})
    monkeypatch.setattr(
        front_app, "_get_context_real",
        lambda tid, req: {"telescopes": list(TELESCOPES), "partial_path": front_app._partial_path(req)},
    )
    assert front_app.get_context(1, _Req("/1/home-content"))["partial_path"] == "home-content"
    context = front_app.get_context(1, _Req("/1/schedule/image?x=1"))
    assert context["partial_path"] == "schedule/image"
    # Home deletes telescopes from what it is given. That must not empty the next page's menu.
    del context["telescopes"]
    assert front_app.get_context(1, _Req("/1/live"))["telescopes"] == TELESCOPES


def test_legacy_pages_use_the_shared_nav_and_load_its_script_and_styles():
    assert "{% include 'ssc/nav.html' %}" in (TEMPLATES / "nav.html").read_text()
    base = (TEMPLATES / "base.html").read_text()
    assert "/public/ssc/nav.css" in base and "/public/ssc/shell.js" in base
    assert '@import url("nav.css")' in (PUBLIC / "ssc" / "ssc.css").read_text()


def test_appearance_offers_four_modes_and_leaves_forms_and_the_server_alone():
    html = _render("ssc/appearance.html")
    assert re.findall(r'data-theme-mode="(\w+)"', html) == ["auto", "day", "night", "red"]
    assert re.findall(r'>(\w+)</button>', html) == ["Auto", "Day", "Night", "Red"]
    assert len(re.findall(r'type="button"', html)) == 4
    assert "name=" not in html and "toggleuitheme" not in html
    shell = (PUBLIC / "ssc" / "shell.js").read_text()
    assert "sscTheme.set(" in shell and "sscTheme.mode()" in shell and "toggleuitheme" not in shell
