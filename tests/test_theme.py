"""Per-device theme: the /roof/sun route and theme.js's public surface."""

from pathlib import Path

import falcon
import falcon.testing
import pytest

THEME_JS = Path(__file__).parent.parent / "front" / "public" / "ssc" / "theme.js"


class _Boom:
    def __getattr__(self, name):
        raise AssertionError("the scope was contacted")


@pytest.fixture
def client(monkeypatch):
    from roof import config as roof_config
    from roof import web as roof_web

    monkeypatch.setattr(roof_web, "get_automation", lambda: _Boom())
    monkeypatch.setattr(
        roof_config, "load", lambda: roof_config.Settings(lat=40.0, lon=-105.0)
    )
    app = falcon.App()
    app.add_route("/roof/sun", roof_web.RoofSun())
    return falcon.testing.TestClient(app)


def test_sun_route_returns_altitude_without_touching_the_scope(client):
    result = client.simulate_get("/roof/sun")
    assert result.status_code == 200
    assert -90 <= result.json["altitude_deg"] <= 90
    assert result.json["known_location"] is True
    assert result.headers["Cache-Control"] == "no-store"


def test_sun_route_is_registered_by_install(monkeypatch):
    from roof import web as roof_web

    monkeypatch.setattr(roof_web, "get_automation", lambda: _Boom())
    monkeypatch.setattr(roof_web.focus, "listen", lambda: None)
    monkeypatch.setattr(roof_web, "_front", lambda: _Boom())
    app = falcon.App()
    from roof import queue_web  # noqa: F401

    class _Auto:
        def start(self):
            pass

    monkeypatch.setattr(roof_web, "get_automation", lambda: _Auto())
    roof_web.install(app)
    assert app._router.find("/roof/sun") is not None


def test_theme_js_keeps_its_public_names():
    source = THEME_JS.read_text()
    for name in ("mode()", "set(next)", "toggleRed()", "sync()", "window.sscTheme"):
        assert name in source
    for mode in ("auto", "day", "night", "red"):
        assert f'"{mode}"' in source
    assert '"ssc-red"' in source  # migration of the old Red flag
    assert "/roof/sun" in source and "data-root-path" in source
