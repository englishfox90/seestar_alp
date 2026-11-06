"""The sky view's dark hours and what it works out from them."""

import json
import math
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import ephem
import pytest

from roof import sky_window

ROOT = Path(__file__).parent.parent
SITE = SimpleNamespace(lat=31.501, lon=-99.3734, sun_max_deg=-18.0, min_target_alt_deg=20.0)
NOW = 1793938000.0  # the evening of 2026-11-05 at the site


def _window(days=0, settings=SITE):
    return sky_window.sky_window(days, lambda when: str(int(when)), now=NOW, settings=settings)


def _node(call):
    # By hand, as front/package.json makes a plain require treat the file as a module.
    source = json.dumps(str(ROOT / "front/public/sky_reach.js"))
    script = (
        "const fs = require('fs'), vm = require('vm'); const window = {};"
        f"vm.runInNewContext(fs.readFileSync({source}, 'utf8'), {{window}});"
        f"const r = window.SkyReach; console.log(JSON.stringify({call}));"
    )
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


def test_the_window_covers_the_dark_hours_in_steps():
    win = _window()
    assert win["known"] and win["floor"] == 20.0 and win["lat"] == SITE.lat
    assert win["night"] == "2026-11-05"
    assert 30 <= len(win["steps"]) <= 60  # roughly 8 to 14 hours of 15 minute steps
    assert all(0 <= step["lst"] < 360 for step in win["steps"])


def test_a_later_night_is_a_different_sky():
    assert _window(90)["night"] == "2027-02-03"
    assert _window(90)["steps"][0]["lst"] != _window()["steps"][0]["lst"]


def test_no_location_means_no_shading():
    win = _window(settings=SimpleNamespace(**{**vars(SITE), "lat": 0.0, "lon": 0.0}))
    assert win["known"] is False and win["steps"] == []


def test_no_full_darkness_gives_no_steps(monkeypatch):
    monkeypatch.setattr(
        sky_window, "night_times",
        lambda *a: {"sunset": None, "sunrise": None, "dark_start": None, "dark_end": None},
    )
    assert _window()["steps"] == []


def test_the_route_is_registered_and_bounded():
    from roof import queue_web

    assert queue_web.PAGES["targets/sky.json"] is queue_web.QueueSky


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_the_browser_sums_match_ephem():
    """The altitude the page works out, against ephem at the same moment.

    The catalog is J2000 and the sidereal time is of the date, so a few tenths of a
    degree apart is expected, and doesn't matter for shading.
    """
    win = _window()  # its clock gives the timestamps back in "at"
    observer = ephem.Observer()
    observer.lat, observer.lon, observer.pressure = str(SITE.lat), str(SITE.lon), 0
    for ra, dec in ((349.0, 60.05), (83.8, -5.39), (200.0, -30.0)):
        body = ephem.FixedBody()
        body._ra, body._dec = math.radians(ra), math.radians(dec)
        for step in (win["steps"][0], win["steps"][-1]):
            when = datetime.fromtimestamp(int(step["at"]), timezone.utc).replace(tzinfo=None)
            observer.date = ephem.Date(when)
            body.compute(observer)
            got = _node(f"r.altAz({SITE.lat}, {ra}, {dec}, {step['lst']})")
            assert got["alt"] == pytest.approx(math.degrees(body.alt), abs=0.6)
            assert got["az"] == pytest.approx(math.degrees(body.az), abs=1.5)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_reach_tells_the_south_from_the_rest():
    win = json.dumps(_window())
    never = _node(f"r.reach({win}, 100, -60)")
    assert never["never"] is True and never["first"] is None
    assert _node("r.ceiling(31.5, -38.5)") == pytest.approx(20.0)
    tonight = _node(f"r.reach({win}, 349, 60)")  # SH2-157 was imaged this very night
    assert tonight["first"] is not None and tonight["never"] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_the_limit_is_a_smooth_line_not_the_grid():
    win = json.dumps(_window())
    shape = _node(f"r.outOfReach({win}, 4)")
    assert shape["lines"], "there is an edge to draw"
    points = [point for line in shape["lines"] for point in line]
    off_grid = [p for p in points if p[0] % 4 and (p[1] + 90) % 4]
    # A crossing sits on one grid line, and lands between the others. Blocks would land on both.
    assert all(not (p[0] % 4) or not ((p[1] + 90) % 4) for p in points) and not off_grid
    assert any((p[0] % 4) > 0.01 for p in points) and any(((p[1] + 90) % 4) > 0.01 for p in points)
    # Segments that touch are joined, so a dashed line runs along it evenly.
    assert max(len(line) for line in shape["lines"]) > 30


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_shading_covers_the_far_south_and_spares_the_imaged_sky():
    win = json.dumps(_window())
    cells = _node(f"r.outOfReach({win}, 4)")["cells"]

    def holds(ra, dec):
        return any(min(p[0] for p in c) <= ra < max(p[0] for p in c) and min(p[1] for p in c) <= dec < max(p[1] for p in c) for c in cells)

    assert holds(100, -80)
    assert not holds(349, 60)  # SH2-157 was imaged this very night
    assert not holds(84, -5)  # M 42
