"""Setting up roof safety from the Automation page, with no file editing."""

import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from roof import config as roof_config
from roof import dashboard, sentinel
from roof import web as roof_web

FORM = {"Content-Type": "application/x-www-form-urlencoded"}


class _Alpaca(BaseHTTPRequestHandler):
    """An Alpaca server with one SafetyMonitor (device 2) and one camera."""

    is_safe = True

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/management/v1/configureddevices":
            value = [
                {
                    "DeviceName": "Roof Sensor",
                    "DeviceType": "SafetyMonitor",
                    "DeviceNumber": 2,
                    "UniqueID": "a",
                },
                {
                    "DeviceName": "Camera",
                    "DeviceType": "Camera",
                    "DeviceNumber": 0,
                    "UniqueID": "b",
                },
            ]
        elif path.endswith("/connected"):
            value = True
        elif path.endswith("/issafe"):
            value = self.is_safe
        else:
            self.send_error(404)
            return
        body = json.dumps({"ErrorNumber": 0, "Value": value}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def alpaca():
    server = HTTPServer(("127.0.0.1", 0), _Alpaca)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Alpaca.is_safe = True
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.mark.parametrize(
    "text, expected",
    [
        ("http://127.0.0.1:11111", "http://127.0.0.1:11111"),
        ("127.0.0.1:11111/", "http://127.0.0.1:11111"),
        ("  https://roof.local  ", "https://roof.local"),
        ("", None),
        ("ftp://x:1", None),
        ("http://x:1/api/v1", None),
        ("http://x:notaport", None),
        ("not a url", None),
    ],
)
def test_normalise_url(text, expected):
    assert sentinel.normalise_url(text) == expected


def test_lists_only_safety_monitors_from_a_server(alpaca):
    assert sentinel.safety_monitors(alpaca) == [
        {"url": alpaca, "device": 2, "name": "Roof Sensor", "local": True}
    ]
    assert sentinel.safety_monitors("http://127.0.0.1:1") == []


def test_discover_asks_this_computer_and_found_servers(monkeypatch, alpaca):
    monkeypatch.setattr(
        sentinel, "LOCAL_SERVERS", ("http://127.0.0.1:1",)
    )  # nothing there
    assert sentinel.discover(servers=[alpaca]) == [
        {"url": alpaca, "device": 2, "name": "Roof Sensor", "local": True}
    ]


def test_discovery_reads_alpaca_replies(monkeypatch):
    """A real UDP exchange with a responder on this computer."""
    import socket

    responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    responder.bind(("127.0.0.1", 0))
    monkeypatch.setattr(sentinel, "DISCOVERY_PORT", responder.getsockname()[1])

    def answer():
        data, addr = responder.recvfrom(64)
        if data == sentinel.DISCOVERY_MESSAGE:
            responder.sendto(b'{"AlpacaPort": 4567}', addr)

    threading.Thread(target=answer, daemon=True).start()
    try:
        assert "http://127.0.0.1:4567" in sentinel._discover_servers(1.0)
    finally:
        responder.close()


# ---- the page ----------------------------------------------------------------------


class _Automation:
    def __init__(self, status):
        self._status = status
        self.requests = []

    def request(self, command):
        self.requests.append(command)

    def note_request(self, text):
        pass

    def start(self):
        pass

    def status(self):
        return self._status


@pytest.fixture
def page(monkeypatch):
    import falcon
    import falcon.testing

    import front.app as front_app

    saved = {}
    base = roof_config.Settings(lat=31.0, lon=-99.0)
    monkeypatch.setattr(
        roof_config, "save", lambda key, value: saved.__setitem__(key, value)
    )
    monkeypatch.setattr(roof_config, "load", lambda: replace(base, **saved))
    messages = []
    monkeypatch.setattr(
        front_app, "flash", lambda resp, message: messages.append(message)
    )
    automation = _Automation(
        {
            "safe": True,
            "safety_detail": "safe",
            "phase": "idle",
            "sched_state": "stopped",
        }
    )
    monkeypatch.setattr(roof_web, "get_automation", lambda: automation)
    monkeypatch.setattr(roof_web, "_found_monitors", None)
    app = falcon.App()
    roof_web.install(app)
    client = falcon.testing.TestClient(app)

    class Page:
        pass

    p = Page()
    p.client, p.saved, p.messages, p.automation, p.base = (
        client,
        saved,
        messages,
        automation,
        base,
    )
    p.post = lambda path, body="": client.simulate_post(path, headers=FORM, body=body)
    return p


def test_use_saves_the_monitor_and_reports_what_it_reads(page, alpaca):
    page.post("/roof/safety/use", f"url={alpaca}&device=2")
    assert page.saved == {"sentinel_url": alpaca, "safety_device": 2}
    assert "reads safe" in page.messages[-1]


def test_use_refuses_a_bad_address_or_device(page):
    page.post("/roof/safety/use", "url=not a url&device=0")
    page.post("/roof/safety/use", "url=http://127.0.0.1:11111&device=-1")
    assert page.saved == {}
    assert all(m.startswith("Save failed") for m in page.messages)


def test_test_button_reports_an_unreachable_monitor(page):
    page.saved.update(sentinel_url="http://127.0.0.1:1", safety_device=0)
    page.post("/roof/safety/test")
    assert page.messages[-1].startswith("Test failed:")


def test_find_lists_what_discovery_found(page, monkeypatch):
    found = [
        {
            "url": "http://10.0.0.5:11111",
            "device": 0,
            "name": "PFR Sentinel Roof",
            "local": False,
        }
    ]
    monkeypatch.setattr(sentinel, "discover", lambda: found)
    page.post("/roof/safety/find")
    assert roof_web._found_monitors == found
    assert page.messages[-1].startswith("Found 1 safety monitor.")


def test_sentinel_web_sets_all_three_urls_and_blank_turns_them_off(page):
    page.post("/roof/safety/sentinel-web", "url=127.0.0.1:1")
    assert page.saved["pier_image_url"] == "http://127.0.0.1:1/latest"
    assert (
        page.saved["pier_status_url"]
        == page.saved["dew_sentinel_url"]
        == "http://127.0.0.1:1/status"
    )
    assert "didn't answer" in page.messages[-1]
    page.post("/roof/safety/sentinel-web", "url=")
    assert page.saved["pier_image_url"] == page.saved["dew_sentinel_url"] == ""


def test_go_live_needs_a_site_and_an_answering_monitor(page):
    page.automation._status["safety_detail"] = "read failed: refused"
    page.post("/roof/mode", "live=true")
    assert "dry_run" not in page.saved
    assert page.messages[-1].startswith("Go live failed")
    page.automation._status["safety_detail"] = (
        "IsSafe is not true"  # answering; unsafe is fine
    )
    page.post("/roof/mode", "live=true")
    assert page.saved["dry_run"] is False


def test_go_live_refused_without_a_site(page):
    page.base = replace(page.base, lat=0.0, lon=0.0)
    roof_config.load = lambda: replace(page.base, **page.saved)
    page.post("/roof/mode", "live=true")
    assert "dry_run" not in page.saved
    assert "latitude" in page.messages[-1]


def test_back_to_dry_run_refused_while_a_session_runs(page):
    page.saved["dry_run"] = False
    page.automation._status.update(phase="imaging", sched_state="working")
    page.post("/roof/mode", "live=false")
    assert page.saved["dry_run"] is False
    assert "Stop & park first" in page.messages[-1]
    page.automation._status.update(phase="idle", sched_state="stopped")
    page.post("/roof/mode", "live=false")
    assert page.saved["dry_run"] is True


def test_page_shows_the_safety_section_and_go_live(page, tmp_path):
    from tests.test_roof import Rig

    rig = Rig(tmp_path)
    rig.tick()  # a full status, as the running automation publishes it
    page.automation._status = rig.auto.status()
    html = page.client.simulate_get("/1/roof").text
    assert 'id="roof-safety"' in html and "Find safety monitors" in html
    assert 'data-open-sheet="roof-live-sheet"' in html
    assert 'href="/1/config"' in html


# ---- pier camera --------------------------------------------------------------------


def test_pier_image_without_a_status_url_still_shows(monkeypatch):
    import cv2
    import numpy as np

    ok, jpeg = cv2.imencode(".jpg", np.zeros((10, 10, 3), np.uint8))

    class Reply:
        content = jpeg.tobytes()
        headers = {}

        def raise_for_status(self):
            pass

    calls = []
    monkeypatch.setattr(
        dashboard.requests, "get", lambda url, timeout: calls.append(url) or Reply()
    )
    store = dashboard.PierStore()
    assert store.latest("http://cam/snapshot.jpg", "") is not None
    assert calls == ["http://cam/snapshot.jpg"]


def test_found_monitors_on_other_computers_carry_a_warning(page, tmp_path, monkeypatch):
    from tests.test_roof import Rig

    rig = Rig(tmp_path)
    rig.tick()
    page.automation._status = rig.auto.status()
    monkeypatch.setattr(
        roof_web,
        "_found_monitors",
        [
            {
                "url": "http://127.0.0.1:11111",
                "device": 0,
                "name": "PFR Sentinel Roof",
                "local": True,
            },
            {
                "url": "http://172.23.20.47:32330",
                "device": 0,
                "name": "N.I.N.A. Safety Monitor",
                "local": False,
            },
        ],
    )
    html = page.client.simulate_get("/1/roof").text
    assert "On this computer: http://127.0.0.1:11111" in html
    assert "can belong to someone else" in html


def test_the_example_files_placeholder_site_counts_as_no_site(monkeypatch):
    from device.config import Config

    values = {"lat": 37.12, "long": -123.45}
    monkeypatch.setattr(
        Config,
        "get_toml",
        lambda section, key, default: values.get(key, default)
        if section == "seestar_initialization"
        else default,
    )
    assert (roof_config.load().lat, roof_config.load().lon) == (0.0, 0.0)
    values.update(lat=31.0, long=-99.0)
    assert (roof_config.load().lat, roof_config.load().lon) == (31.0, -99.0)


def test_site_settings_save_building_and_folder(page, tmp_path):
    page.post("/roof/site", f"building=12&image_dir={tmp_path}")
    assert page.saved == {"site_building": 12, "discord_image_dir": str(tmp_path)}
    page.post("/roof/site", "building=0&image_dir=")
    assert page.saved == {"site_building": 0, "discord_image_dir": ""}


def test_site_settings_refuse_a_missing_folder_or_bad_building(page, tmp_path):
    page.post("/roof/site", f"building=12&image_dir={tmp_path}/nope")
    page.post("/roof/site", "building=-3&image_dir=")
    assert page.saved == {}
    assert all(m.startswith("Save failed") for m in page.messages)
