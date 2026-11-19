"""Richer Discord posts: cards with fields and a health footer, health warnings, the
briefing before dark and the report after dawn."""

import io
from dataclasses import replace

import pytest
from PIL import Image

from roof import alert_images, night_brief, planner, schedule_tools
from roof import config as roof_config
from roof.alerts import DEW_WARN_S, NightAlerts, deliver, health_text, payload
from roof.queue import Target
from roof.scope import ScopeState, health_from
from roof.sessions import SessionLog
from tests.test_roof_alerts import RecordingSender

NIGHT = "2026-11-18"
SITE = dict(lat=31.0, lon=-99.0)
HEALTHY = {"battery": 100, "charging": True, "charger": "Full", "temp_c": 32.0,
           "overtemp": False, "battery_overtemp": False, "free_gb": 85.0, "total_gb": 102.6}  # fmt: skip


def _state(**health):
    return ScopeState(
        connected=True, parked=True, sched_state="stopped", health={**HEALTHY, **health}
    )


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _alerts(tmp_path, clock=None, muted=(), limits=None, **kw):
    sender = RecordingSender()
    alerts = NightAlerts(
        sender,
        state_path=str(tmp_path / "alerts.json"),
        clock=clock or Clock(),
        muted=lambda: muted,
        limits=lambda: limits
        or {"battery_pct": 20, "storage_gb": 5, "briefing_lead_min": 60},
        **kw,
    )
    return alerts, sender


def _observe(alerts, state, dark=False, night=NIGHT, dark_start=None):
    alerts.observe(state, dark, night, "idle", "", (0, 0), False, dark_start)


# ---- readings and formatting ----------------------------------------------------------


def test_health_from_reads_the_scopes_device_state():
    health = health_from(
        {
            "pi_status": {
                "temp": 32.0,
                "charger_status": "Full",
                "battery_capacity": 100,
                "charge_online": True,
                "is_overtemp": False,
                "battery_overtemp": False,
            },  # fmt: skip
            "storage": {
                "cur_storage": "emmc",
                "storage_volume": [
                    {"name": "emmc", "free_mb": 86978, "total_mb": 105066}
                ],
            },
        }
    )
    assert (
        health["battery"] == 100
        and health["charging"] is True
        and health["temp_c"] == 32.0
    )
    assert health["free_gb"] == pytest.approx(84.94, abs=0.01)
    assert health_from({}) is None and health_from({"device": {}}) is None


def test_health_text_reads_like_a_footer():
    assert health_text(HEALTHY) == "Battery 100%, on power · 32 °C · 85 GB free"
    assert health_text({**HEALTHY, "charger": "Charging", "battery": 80}).startswith(
        "Battery 80%, charging"
    )
    assert health_text({**HEALTHY, "charging": False, "battery": 40}).startswith(
        "Battery 40%, not charging"
    )
    assert health_text(None) == ""


def test_payload_carries_fields_footer_and_picture():
    body = payload(
        "T",
        "text",
        "info",
        0,
        [{"name": "A", "value": "1", "inline": True}, {"name": "B", "value": ""}],
        "foot",
        "x.png",
    )
    embed = body["embeds"][0]
    assert embed["fields"] == [
        {"name": "A", "value": "1", "inline": True}
    ]  # an empty field is left out
    assert embed["footer"] == {"text": "foot"} and embed["image"] == {
        "url": "attachment://x.png"
    }


def test_deliver_posts_a_named_picture():
    calls = []

    class Reply:
        status_code = 200

    deliver("https://discord.com/api/webhooks/1/x", {}, post=lambda url, **k: calls.append(k) or Reply(),
            photo=("tonight.png", b"png", "image/png"))  # fmt: skip
    assert calls[0]["files"]["files[0]"] == ("tonight.png", b"png", "image/png")


# ---- cards ---------------------------------------------------------------------------


def test_cards_carry_a_health_footer_only_while_fresh(tmp_path):
    clock = Clock()
    alerts, sender = _alerts(tmp_path, clock)
    _observe(alerts, _state())
    alerts.alarm("something")
    assert sender.extras[-1]["footer"] == "Battery 100%, on power · 32 °C · 85 GB free"
    clock.t += 3600
    alerts.alarm("something else")
    assert sender.extras[-1]["footer"] is None


def test_session_start_card_has_fields_and_the_goal(tmp_path):
    alerts, sender = _alerts(
        tmp_path, goal_for=lambda name: "3.1 of 8 h" if name == "A" else None
    )
    alerts.session_started("Target list", ["A", "B"], 1, False, NIGHT)
    fields = {f["name"]: f["value"] for f in sender.extras[-1]["fields"]}
    assert fields == {
        "Schedule": "Target list",
        "First up": "A (target 1 of 2)",
        "Goal": "3.1 of 8 h",
        "Then": "B",
    }


# ---- health warnings -------------------------------------------------------------------


def test_low_battery_warns_once_while_not_charging_and_rearms(tmp_path):
    alerts, sender = _alerts(tmp_path)
    _observe(alerts, _state(battery=30, charging=False))
    assert "Scope stopped charging" not in sender.titles()  # it was never seen charging
    _observe(alerts, _state(battery=19, charging=False))
    _observe(alerts, _state(battery=18, charging=False))
    assert sender.titles().count("Scope battery low") == 1
    _observe(alerts, _state(battery=18, charging=True))  # plugged back in: re-armed
    _observe(alerts, _state(battery=17, charging=False))
    assert sender.titles().count("Scope battery low") == 2


def test_charging_stopping_warns(tmp_path):
    alerts, sender = _alerts(tmp_path)
    _observe(alerts, _state(charging=True))
    _observe(alerts, _state(charging=False, battery=97))
    title, text, level = sender.find("Scope stopped charging")
    assert "97%" in text and level == "warning"


def test_overheating_and_low_storage_warn_once(tmp_path):
    alerts, sender = _alerts(tmp_path)
    for _ in range(3):
        _observe(alerts, _state(overtemp=True, free_gb=3.2))
    assert sender.titles().count("Scope overheating") == 1
    assert sender.titles().count("Scope storage low") == 1
    assert "3.2 GB free" in sender.find("Scope storage low")[1]


def test_health_warnings_can_be_switched_off(tmp_path):
    alerts, sender = _alerts(tmp_path, muted=("battery", "temperature", "storage"))
    _observe(alerts, _state(battery=5, charging=False, overtemp=True, free_gb=1.0))
    assert sender.sent == []


def test_dew_warns_after_fifteen_minutes_at_the_dew_point_with_the_heater_on(tmp_path):
    clock = Clock()
    alerts, sender = _alerts(tmp_path, clock)
    alerts.dew(-0.3, True)
    clock.t += DEW_WARN_S - 60
    alerts.dew(-0.3, True)
    assert sender.sent == []
    clock.t += 120
    alerts.dew(-0.4, True)
    alerts.dew(-0.4, True)
    assert sender.titles() == ["Dew point reached"]
    alerts.dew(
        -0.4, False
    )  # heater off resets the wait, and it said so once tonight anyway


# ---- the briefing ----------------------------------------------------------------------


def test_briefing_posts_once_in_its_window_and_keeps_the_forecast(tmp_path):
    clock = Clock()
    calls = []

    def build(night, now, health):
        calls.append((night, health))
        return {"title": "Tonight: Roof likely open", "text": "Dark 19:01 to 05:46", "level": "success",
                "fields": [], "image": ("tonight.png", b"png", "image/png"),
                "forecast": {"headline": "Roof likely open", "open_h": 9.5}}  # fmt: skip

    alerts, sender = _alerts(tmp_path, clock, briefing=build)
    dark = clock.t + 2 * 3600
    _observe(alerts, _state(), dark_start=dark)
    assert calls == []  # two hours early; the lead is one hour
    clock.t = dark - 3000
    _observe(alerts, _state(), dark_start=dark)
    _observe(alerts, _state(), dark_start=dark)
    assert len(calls) == 1 and calls[0][1]["battery"] == 100
    assert sender.titles() == ["Tonight: Roof likely open"]
    assert sender.extras[-1]["image"][0] == "tonight.png"
    assert alerts._night["forecast"] == {"headline": "Roof likely open", "open_h": 9.5}


def test_a_muted_briefing_is_not_built(tmp_path):
    calls = []
    alerts, sender = _alerts(
        tmp_path, muted=("briefing",), briefing=lambda *a: calls.append(a)
    )
    _observe(alerts, _state(), dark_start=alerts._clock() + 600)
    assert calls == [] and sender.sent == []


def test_briefing_reports_the_plan_moon_and_a_timeline(monkeypatch, tmp_path):
    settings = roof_config.Settings(
        **SITE, schedule=schedule_tools.TARGET_LIST, enabled=True
    )
    log = SessionLog(str(tmp_path / "sessions.json"))
    targets = [Target(id="a", name="M 42", ra=5.588, dec=-5.39, goal_hours=6.0)]
    window = planner.night_times(
        NIGHT, settings.lat, settings.lon, settings.sun_max_deg
    )
    hours = [{"start": window["sunset"] + k * 3600, "end": window["sunset"] + (k + 1) * 3600, "state": "open"}
             for k in range(14)]  # fmt: skip
    outlook = {"night": NIGHT, "headline": "Roof likely open", "tone": "ok", "key": "open", "detail": "Clear all night",
               "hours": hours, "dew": "Dew point stays well below"}  # fmt: skip
    src = night_brief.Sources(
        settings=lambda: settings,
        clock=lambda t: __import__("datetime")
        .datetime.fromtimestamp(t)
        .strftime("%H:%M"),
        tz=lambda: None,
        targets=lambda: targets,
        log=lambda: log,
        history=lambda: None,
        outlook=lambda s, now: {"nights": [outlook]},
        open_chance=lambda s, now: None,
        image_dir=lambda: "",
    )
    monkeypatch.setattr(
        planner,
        "config_from_settings",
        lambda s: planner.PlanConfig(lat=s.lat, lon=s.lon, sun_max_deg=s.sun_max_deg),
    )
    message = night_brief.briefing(NIGHT, window["sunset"], HEALTHY, src)
    assert (
        message["title"] == "Tonight: Roof likely open"
        and message["level"] == "success"
    )
    assert (
        message["text"].startswith("Dark ")
        and "Moon " in message["text"]
        and "Dew point" in message["text"]
    )
    fields = {f["name"]: f["value"] for f in message["fields"]}
    assert "M 42" in fields["Plan"] and "(0.0 of 6 h)" in fields["Plan"]
    assert fields["Scope"].startswith("Battery 100%")
    name, png, mime = message["image"]
    assert (
        mime == "image/png"
        and Image.open(io.BytesIO(png)).size[0] == alert_images.WIDTH
    )
    assert message["forecast"]["open_h"] == pytest.approx(
        (window["dark_end"] - window["dark_start"]) / 3600, abs=0.01
    )

    closed = {
        **outlook,
        "headline": "Roof likely closed",
        "key": "closed",
        "tone": "danger",
    }
    src.outlook = lambda s, now: {"nights": [closed]}
    message = night_brief.briefing(NIGHT, window["sunset"], None, src)
    assert (
        message["text"].startswith("No imaging expected tonight.")
        and message["level"] == "warning"
    )


def test_briefing_needs_a_site():
    src = night_brief.Sources(*(lambda *a: None for _ in range(9)))
    src.settings = lambda: roof_config.Settings()
    assert night_brief.briefing(NIGHT, 0, None, src) is None


# ---- the morning report ----------------------------------------------------------------


def test_dawn_hands_the_night_to_the_report_builder(tmp_path):
    clock = Clock()
    seen = []

    def report(state, now, health):
        seen.append(state)
        return {
            "title": "Night of Wed 18 Nov: 1.0 h on 1 target",
            "text": "",
            "level": "success",
            "fields": [],
        }

    alerts, sender = _alerts(tmp_path, clock, report=report)
    _observe(alerts, _state(), dark=True)
    alerts.alarm("framing lost")
    _observe(alerts, _state(), dark=False)  # dawn
    assert sender.titles()[-1] == "Night of Wed 18 Nov: 1.0 h on 1 target"
    assert seen[0]["alarms"][0]["message"] == "framing lost"


def _jpg(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (300, 200), (40, 60, 90)).save(path)


def test_report_has_targets_goal_roof_alarms_and_a_collage(tmp_path):
    settings = roof_config.Settings(**SITE, schedule="Good.json")
    window = planner.night_times(
        NIGHT, settings.lat, settings.lon, settings.sun_max_deg
    )
    log = SessionLog(str(tmp_path / "sessions.json"))
    targets = [
        Target(
            id="a", name="M 42", ra=5.588, dec=-5.39, goal_hours=6.0, prior_hours=2.0
        )
    ]
    started = window["dark_start"] + 600
    _jpg(
        tmp_path
        / "MyWorks"
        / "M 42"
        / "Stacked_100_M 42_30.0s_IRCUT_20261119-010000.jpg"
    )
    import os

    os.utime(
        tmp_path
        / "MyWorks"
        / "M 42"
        / "Stacked_100_M 42_30.0s_IRCUT_20261119-010000.jpg",
        (started + 7200,) * 2,
    )

    class History:
        def roof_record(self, night, lon, now):
            return {
                "closed": [
                    {
                        "start": window["dark_start"],
                        "end": window["dark_start"] + 3600,
                        "open": False,
                    }
                ],
                "unknown": [],
            }

    src = night_brief.Sources(
        settings=lambda: settings,
        clock=lambda t: "01:00",
        tz=lambda: None,
        targets=lambda: targets,
        log=lambda: log,
        history=lambda: History(),
        outlook=lambda s, now: None,
        open_chance=lambda s, now: None,
        image_dir=lambda: str(tmp_path / "MyWorks"),
    )
    state = {"night": NIGHT, "imaged": True, "sessions": 1,
             "targets": {"M 42": {"frames": 240, "dropped": 10, "seconds": 7200.0}},
             "stacks": [{"name": "M 42", "started": started}],
             "alarms": [{"at": started, "message": "framing lost"}],
             "forecast": {"headline": "Roof likely open", "open_h": 10.0}}  # fmt: skip
    message = night_brief.morning_report(state, window["dark_end"] + 60, HEALTHY, src)
    assert message["title"] == "Night of Wed 18 Nov: 2.0 h on 1 target"
    fields = {f["name"]: f["value"] for f in message["fields"]}
    assert (
        fields["M 42"]
        == "240 frames, 2.0 h\n10 dropped (4%)\nGoal 2.0 of 6 h (+2.0 tonight)"
    )
    dark_h = (window["dark_end"] - window["dark_start"]) / 3600
    assert (
        fields["Roof"]
        == f"Open {dark_h - 1:.1f} h of {dark_h:.1f} h dark\nForecast said: Roof likely open (10.0 h open)"
    )
    assert fields["Alarms"] == "01:00  framing lost"
    assert message["level"] == "warning"  # an alarm tempers a good night
    name, jpg, mime = message["image"]
    assert (
        mime == "image/jpeg"
        and Image.open(io.BytesIO(jpg)).size == (alert_images.COLLAGE_TILE,) * 2
    )


def test_report_of_a_night_that_never_started_says_why():
    settings = roof_config.Settings()
    src = night_brief.Sources(*(lambda *a: None for _ in range(9)))
    src.settings, src.image_dir, src.targets = (
        (lambda: settings),
        (lambda: ""),
        (lambda: []),
    )
    message = night_brief.morning_report(
        {"night": NIGHT, "last_note": "waiting: roof not safe"}, 0, None, src
    )
    assert (
        message["title"] == "Night of Wed 18 Nov: no imaging"
        and message["text"] == "Roof not safe."
    )


def test_collage_skips_unreadable_files(tmp_path):
    _jpg(tmp_path / "a.jpg")
    (tmp_path / "b.jpg").write_bytes(b"not an image")
    sheet = alert_images.collage(
        [("A", str(tmp_path / "a.jpg")), ("B", str(tmp_path / "b.jpg"))]
    )
    assert Image.open(io.BytesIO(sheet)).size == (alert_images.COLLAGE_TILE,) * 2
    assert alert_images.collage([("B", str(tmp_path / "b.jpg"))]) is None


# ---- wiring ------------------------------------------------------------------------------


def test_automation_passes_tonights_dark_start(tmp_path):
    from tests.test_roof import Rig

    rig = Rig(tmp_path)
    s = replace(rig.settings, **SITE)
    expected = planner.night_times(NIGHT, 31.0, -99.0, -18.0)["dark_start"]
    assert rig.auto._dark_start(NIGHT, s) == expected
    assert rig.auto._dark_start(NIGHT, replace(s, lat=0.0, lon=0.0)) is None
