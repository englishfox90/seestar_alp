"""The Target List's sky view: adding with framing, and saving framing changes."""

import falcon
import falcon.testing
import pytest

from roof import queue_web
from roof import web as roof_web
from roof.queue import Target

FORM = {"Content-Type": "application/x-www-form-urlencoded"}


class _Front:
    def flash(self, resp, message):
        self.message = message

    def redirect(self, location):
        raise falcon.HTTPFound(location)


@pytest.fixture
def client(monkeypatch, tmp_path):
    front = _Front()
    monkeypatch.setattr(queue_web, "_base", lambda: str(tmp_path))
    monkeypatch.setattr(roof_web, "_front", lambda: front)
    app = falcon.App()
    app.req_options.auto_parse_form_urlencoded = False
    for path, resource in queue_web.PAGES.items():
        app.add_route(f"/{path}", resource())
    client = falcon.testing.TestClient(app)
    client.front = front
    return client


def _listed(**fields):
    target = Target(id="abc", name="NGC 6960", ra=20.87, dec=30.48, **fields)
    queue_web._queue()._write([target])
    return target


def _post(client, path, body):
    return client.simulate_post(path, headers=FORM, body=body)


def _only():
    return queue_web._queue().all()[0]


def test_adding_with_framing_uses_the_scope_estimate(client):
    _post(client, "/targets/add", "name=NGC+7000&ra=20.97&dec=44.5&goal_hours=8&mosaic_scale=1.7&mosaic_angle=30")
    target = _only()
    assert (target.mosaic_scale, target.mosaic_angle) == (1.7, 30.0)
    assert target.framed and target.min_session_hours == 1.375
    assert target.goal_hours == 8.0


def test_adding_at_scale_one_is_a_single_frame(client):
    _post(client, "/targets/add", "name=M+42&ra=5.59&dec=-5.39&mosaic_scale=1.0&mosaic_angle=0")
    assert not _only().framed and _only().min_session_hours == 1.0


def test_adding_without_framing_still_works(client):
    assert _post(client, "/targets/add", "name=M+42&ra=5.59&dec=-5.39").status_code == 302
    assert not _only().framed


def test_saving_framing_keeps_goal_and_settings(client):
    _listed(goal_hours=30.0, priority="high", prior_hours=2.0, min_session_hours=3.0, mosaic_scale=1.7)
    _post(client, "/targets/framing", "id=abc&ra=20.9&dec=31&mosaic_scale=2&mosaic_angle=-15")
    target = _only()
    assert (target.ra, target.dec, target.mosaic_scale, target.mosaic_angle) == (20.9, 31.0, 2.0, -15.0)
    assert (target.goal_hours, target.priority, target.prior_hours) == (30.0, "high", 2.0)
    assert target.min_session_hours == 3.0


def test_star_map_angle_keeps_its_offset(client):
    _listed(mosaic_scale=1.7, mosaic_angle=10.0, mosaic_star_map_angle=12.054)
    _post(client, "/targets/framing", "id=abc&ra=20.9&dec=31&mosaic_scale=1.7&mosaic_angle=25")
    assert _only().mosaic_star_map_angle == pytest.approx(27.054)


def test_star_map_angle_stays_unset_without_one(client):
    _listed(mosaic_scale=1.7, mosaic_angle=10.0)
    _post(client, "/targets/framing", "id=abc&ra=20.9&dec=31&mosaic_scale=1.7&mosaic_angle=25")
    assert _only().mosaic_star_map_angle is None


def test_framing_a_single_frame_target_gets_a_framed_shortest_run(client):
    _listed(min_session_hours=1.0)
    _post(client, "/targets/framing", "id=abc&ra=20.9&dec=31&mosaic_scale=1.7&mosaic_angle=0")
    assert _only().min_session_hours == 1.375


def test_saving_warns_when_the_shortest_run_is_under_the_estimate(client):
    _listed(min_session_hours=1.0, mosaic_scale=1.7)
    _post(client, "/targets/framing", "id=abc&ra=20.9&dec=31&mosaic_scale=1.7&mosaic_angle=0")
    assert "1.375 h estimate" in client.front.message


def test_saving_a_removed_target_says_so(client):
    assert _post(client, "/targets/framing", "id=gone&ra=1&dec=1").status_code == 302
    assert "no longer listed" in client.front.message


@pytest.mark.parametrize(
    "body",
    [
        "id=abc&ra=20.9&dec=31&mosaic_scale=0.5&mosaic_angle=0",
        "id=abc&ra=20.9&dec=31&mosaic_scale=5&mosaic_angle=0",
        "id=abc&ra=20.9&dec=31&mosaic_scale=1.7&mosaic_angle=91",
        "id=abc&ra=20.9&dec=31&mosaic_scale=big&mosaic_angle=0",
        "id=abc&ra=25&dec=31&mosaic_scale=1.7&mosaic_angle=0",
        "id=abc&ra=20.9&dec=95&mosaic_scale=1.7&mosaic_angle=0",
    ],
)
def test_bad_framing_is_refused_and_changes_nothing(client, body):
    _listed(mosaic_scale=1.7, mosaic_angle=10.0)
    assert _post(client, "/targets/framing", body).status_code == 400
    assert (_only().mosaic_scale, _only().mosaic_angle, _only().ra) == (1.7, 10.0, 20.87)


def test_cross_origin_framing_is_refused(client):
    _listed()
    result = client.simulate_post(
        "/targets/framing", headers={"Origin": "http://evil.example", "Host": "localhost:5432"}
    )
    assert result.status_code == 403


def test_sky_data_gives_ra_in_degrees(client):
    _listed(mosaic_scale=1.7, mosaic_angle=10.0)
    row = queue_web._sky_targets()[0]
    assert row["ra"] == pytest.approx(313.05)
    assert (row["dec"], row["scale"], row["angle"]) == (30.48, 1.7, 10.0)


