"""Discord alerts: what gets posted through a night, and that it can't hurt the loop."""

import json
import threading

import pytest
import requests

from roof import alerts as roof_alerts
from roof import focus
from roof.alerts import DiscordSender, NightAlerts, deliver, redact
from roof.automation import IDLE, RoofAutomation
from tests.test_roof import SAFE, UNSAFE, Rig

URL = "https://discord.com/api/webhooks/123456/s3cret-token"
STACKING = {"view_state": "working", "view_stage": "Stack"}


class RecordingSender:
    def __init__(self):
        self.sent = []

    def send(self, title, text, level="info"):
        self.sent.append((title, text, level))

    def titles(self):
        return [title for title, _, _ in self.sent]

    def find(self, title):
        return next(m for m in self.sent if m[0] == title)


class AlertRig(Rig):
    def build(self):
        self.sender = RecordingSender()
        self.alerts = NightAlerts(
            self.sender, state_path=self.state_path + ".alerts", clock=lambda: self.now
        )
        auto = super().build()
        auto._alerts = self.alerts
        return auto

    def stack(self, target, item, stacked, dropped=0, exposure_s=30.0, advance=10):
        self.set_scope(
            target=target,
            item_number=item,
            stacked=stacked,
            dropped=dropped,
            exposure_s=exposure_s,
            **STACKING,
        )
        self.tick(advance)


@pytest.fixture
def rig(tmp_path):
    return AlertRig(tmp_path)


def _reply(status, body=None):
    class Reply:
        status_code = status

        def json(self):
            if body is None:
                raise ValueError
            return body

    return Reply()


# ---- sending -------------------------------------------------------------------


def test_deliver_waits_out_a_rate_limit():
    replies = [_reply(429, {"retry_after": 2.5}), _reply(204)]
    waits = []
    sent, _ = deliver(URL, {}, post=lambda *a, **k: replies.pop(0), sleep=waits.append)
    assert sent and waits == [2.5]


def test_deliver_does_not_retry_a_bad_webhook():
    calls = []

    def post(*a, **k):
        calls.append(a)
        return _reply(404)

    assert deliver(URL, {}, post=post, sleep=lambda s: None) == (False, "HTTP 404")
    assert len(calls) == 1


def test_a_failed_post_never_logs_the_webhook(caplog):
    def post(*a, **k):
        raise requests.ConnectionError(f"Max retries with url: {URL[len('https://discord.com'):]}")

    sender = DiscordSender(lambda: URL, post=post, sleep=lambda s: None)
    sender._queue.append((URL, roof_alerts.payload("T", "x", "info", 0)))
    with caplog.at_level("INFO", logger="roof"):
        sender.drain()
    assert "dropped" in caplog.text
    assert "s3cret-token" not in caplog.text
    assert "s3cret-token" not in redact(URL)


def test_send_returns_at_once_while_the_network_hangs():
    release, posted = threading.Event(), threading.Event()

    def post(*a, **k):
        release.wait(5)
        posted.set()
        return _reply(204)

    sender = DiscordSender(lambda: URL, post=post)
    sender.send("T", "x")  # would hang here if it posted on the caller's thread
    assert not posted.is_set()
    release.set()
    assert posted.wait(5)


def test_the_queue_drops_the_oldest_rather_than_growing():
    sender = DiscordSender(lambda: URL)
    sender._wake.set = lambda: None
    sender._thread = object()  # no worker, so nothing drains
    for number in range(roof_alerts.QUEUE_MAX + 5):
        sender.send(str(number), "x")
    assert len(sender._queue) == roof_alerts.QUEUE_MAX
    assert sender._queue[0][1]["embeds"][0]["title"] == "5"


def test_no_webhook_means_nothing_is_queued():
    sender = DiscordSender(lambda: "")
    sender.send("T", "x")
    assert not sender._queue and sender._thread is None


def test_payload_cannot_ping_and_respects_discords_limits():
    body = roof_alerts.payload("T" * 500, "x" * 5000, "error", 0)
    assert body["allowed_mentions"] == {"parse": []}
    assert len(body["embeds"][0]["title"]) == roof_alerts.TITLE_MAX
    assert len(body["embeds"][0]["description"]) == roof_alerts.TEXT_MAX
    assert body["username"] == "Seestar"


def test_settings_default_to_off():
    from roof.config import Settings

    assert Settings().discord_enabled is False


# ---- a night's story -----------------------------------------------------------


def test_session_start_names_the_plan(rig):
    rig.settle_and_start()
    title, text, level = rig.sender.find("Imaging session started")
    assert level == "success"
    assert "Good.json" in text or "Good" in text
    assert "First up: A (target 1 of 2)" in text and "Then: B" in text


def test_stacking_is_announced_once_per_target_at_the_first_frame(rig):
    rig.settle_and_start()
    rig.set_scope(target="A", item_number=2, view_state="working", view_stage="AutoGoto")
    rig.tick(10)
    assert "Stacking A" not in rig.sender.titles()
    rig.stack("A", 2, 1)
    rig.stack("A", 2, 2)
    assert rig.sender.titles().count("Stacking A") == 1
    assert "30 s exposures" in rig.sender.find("Stacking A")[1]


def test_switching_target_reports_the_last_targets_frames(rig):
    rig.settle_and_start()
    rig.stack("A", 2, 100, dropped=7)
    rig.stack("B", 3, 1)
    title, text, level = rig.sender.find("Switching target: B")
    assert level == "info"
    assert "target 2 of 2" in text
    assert "A finished: 100 frames, 50 min, 7 dropped" in text


def test_a_mosaics_panels_add_up_when_the_firmware_count_restarts(rig):
    rig.settle_and_start()
    rig.stack("A", 2, 50)
    rig.stack("A", 2, 3)  # next panel
    rig.stack("A", 2, 10)
    rig.stack("B", 3, 1)
    assert "A finished: 60 frames" in rig.sender.find("Switching target: B")[1]


def test_a_target_that_stacked_nothing_is_flagged(rig):
    rig.settle_and_start()
    rig.set_scope(target="A", item_number=2)
    rig.tick(10)
    rig.set_scope(target="B", item_number=3)
    rig.tick(10)
    title, text, level = rig.sender.find("Switching target: B")
    assert level == "warning" and "A stacked nothing" in text


def test_a_low_target_says_it_was_skipped_rather_than_failed(tmp_path):
    rig = AlertRig(tmp_path, min_target_alt_deg=20.0)
    rig.settle_and_start()
    rig.set_scope(target="A", item_number=2)
    rig.tick(10)
    rig.alts["A"] = 10.0
    rig.tick(10)
    rig.alts["A"] = 60.0
    rig.set_scope(target="B", item_number=3)
    rig.tick(10)
    text = rig.sender.find("Switching target: B")[1]
    assert "A skipped: below 20 deg" in text
    assert "probably failed" not in text


def test_a_settings_item_with_a_stale_view_name_is_not_a_target_switch(rig):
    rig.settle_and_start()  # item 1 is a settings item
    rig.set_scope(target="last night's target", item_number=1)
    rig.tick(10)
    rig.stack("A", 2, 1)
    assert not [t for t in rig.sender.titles() if t.startswith("Switching")]
    assert "Stacking A" in rig.sender.titles()


def test_session_end_gives_the_reason_and_frames_per_target(rig):
    rig.settle_and_start()
    rig.stack("A", 2, 100)
    rig.stack("B", 3, 20)
    rig.safety = UNSAFE
    rig.tick()
    rig.tick(31)
    rig.tick(10)
    title, text, level = rig.sender.find("Imaging stopped: roof unsafe")
    assert level == "warning"
    assert "A: 100 frames, 50 min" in text and "B: 20 frames, 10 min" in text
    assert rig.sender.titles().count("Imaging stopped: roof unsafe") == 1


def test_the_roof_itself_is_never_announced(rig):
    # Sentinel posts the roof opening and closing.
    rig.tick()
    rig.safety = UNSAFE
    rig.tick(40)
    rig.tick(40)
    assert rig.sender.sent == []


def test_finished_schedule_then_park(rig):
    rig.settle_and_start()
    rig.stack("A", 2, 100)
    rig.set_scope(sched_state="complete")
    rig.tick(10)
    assert rig.sender.find("Imaging session finished")[2] == "success"
    rig.set_scope(parked=True)
    rig.tick(10)
    assert "Arm parked" in rig.sender.titles()
    assert rig.auto.phase == IDLE


def test_a_session_started_elsewhere_is_followed(rig):
    rig.set_scope(sched_state="working", target="X", item_number=1, parked=False)
    rig.tick()
    assert "Imaging session started elsewhere" in rig.sender.titles()
    rig.stack("X", 1, 5)
    rig.stack("Y", 2, 1)
    assert "Switching target: Y" in rig.sender.titles()


def test_a_resumed_session_says_so(rig):
    rig.settle_and_start()
    rig.set_scope(item_number=3, target="B")
    rig.tick(10)
    rig.safety = UNSAFE
    rig.tick()
    rig.tick(31)
    rig.set_scope(parked=True, sched_state="stopped", item_number=0)
    rig.tick(10)
    rig.safety = SAFE
    rig.tick(10)
    rig.tick(121)
    rig.set_scope(sched_state="complete")
    rig.tick(10)
    text = rig.sender.find("Imaging session resumed")[1]
    assert "First up: B (target 2 of 2)" in text


def test_alarms_are_posted_and_repeats_held_back(rig):
    rig.auto._raise_alarm("PARK NOT CONFIRMED")
    rig.auto._raise_alarm("PARK NOT CONFIRMED")
    assert rig.sender.titles() == ["Seestar alarm"]
    rig.now += roof_alerts.REPEAT_ALARM_S + 1
    rig.auto._raise_alarm("PARK NOT CONFIRMED")
    assert rig.sender.titles() == ["Seestar alarm", "Seestar alarm"]
    assert rig.sender.sent[0][2] == "error"


def test_lost_contact_during_a_session_is_said_once_then_recovery(rig):
    rig.settle_and_start()
    rig.set_scope(connected=False, sched_state="unknown")
    rig.tick(60)
    assert "Lost contact with the scope" not in rig.sender.titles()
    rig.tick(70)
    rig.tick(70)
    assert rig.sender.titles().count("Lost contact with the scope") == 1
    rig.set_scope(connected=True, sched_state="working")
    rig.tick(10)
    assert "Back in contact with the scope" in rig.sender.titles()


# ---- autofocus -----------------------------------------------------------------


def _focus_events(log, *ending, points=((1000, 2.0), (1100, 5.0), (1090, 5.2)), curve=(0.1, -2, 3)):
    log.on_event({"Event": "AutoFocus", "state": "start"})
    log.on_event(
        {
            "Event": "AutoFocus",
            "state": "working",
            "result": {"points": [list(p) for p in points], "curve": list(curve)},
        }
    )
    for event in ending:
        log.on_event({"Event": "AutoFocus", **event})


@pytest.fixture
def focus_rig(rig):
    log = focus.FocusLog()
    log.on_finish = rig.alerts.autofocus
    rig.focus = log
    rig.settle_and_start()
    rig.set_scope(target="A", item_number=2)
    rig.tick(10)
    return rig


def test_autofocus_pass_is_reported_once_despite_two_complete_events(focus_rig):
    _focus_events(
        focus_rig.focus,
        {"state": "complete", "result": {"last_point": [0, 0.0]}},
        {"state": "complete", "lapse_ms": 60933},
    )
    title = "Autofocus passed on A"
    assert focus_rig.sender.titles().count(title) == 1
    assert "step 1090" in focus_rig.sender.find(title)[1]


def test_autofocus_failure_carries_the_scopes_error(focus_rig):
    _focus_events(focus_rig.focus, {"state": "fail", "error": "no star is detected"})
    _, text, level = focus_rig.sender.find("Autofocus failed on A")
    assert level == "error" and "no star is detected" in text


def test_autofocus_cancel_from_stopping_a_view_says_nothing(focus_rig):
    _focus_events(focus_rig.focus, {"state": "cancel"})
    assert not [t for t in focus_rig.sender.titles() if t.startswith("Autofocus")]


def test_autofocus_with_an_all_zero_fit_is_not_called_a_pass(focus_rig):
    _focus_events(focus_rig.focus, {"state": "complete"}, curve=(0, 0, 0))
    assert focus_rig.sender.find("Autofocus gave no usable fit on A")[2] == "warning"


def test_autofocus_outside_a_session_is_not_posted(rig):
    log = focus.FocusLog()
    log.on_finish = rig.alerts.autofocus
    _focus_events(log, {"state": "complete"})
    assert rig.sender.sent == []


def test_stacking_after_a_failed_focus_warns(focus_rig):
    _focus_events(focus_rig.focus, {"state": "fail", "error": "no star"})
    focus_rig.stack("A", 2, 1)
    _, text, level = focus_rig.sender.find("Stacking A")
    assert level == "warning" and "autofocus failed" in text


def test_stacking_after_a_retry_that_passed_does_not_warn(focus_rig):
    _focus_events(focus_rig.focus, {"state": "fail", "error": "no star"})
    _focus_events(focus_rig.focus, {"state": "complete"})
    focus_rig.stack("A", 2, 1)
    assert focus_rig.sender.find("Stacking A")[2] == "success"


# ---- a night with no imaging ---------------------------------------------------


def _wait_in_the_dark(rig, seconds):
    rig.safety = UNSAFE
    rig.tick()
    rig.now += seconds
    rig.tick()


def test_not_imaging_yet_says_why_once(rig):
    _wait_in_the_dark(rig, roof_alerts.WAIT_ALERT_S - 60)
    assert "Not imaging yet" not in rig.sender.titles()
    rig.tick(61)
    rig.tick(600)
    assert rig.sender.titles() == ["Not imaging yet"]
    assert rig.sender.sent[0][1] == "Roof not safe."


def test_dawn_with_no_session_says_no_imaging_tonight(rig):
    _wait_in_the_dark(rig, 60)
    rig.sun = -10.0
    rig.tick(10)
    rig.tick(10)
    _, text, level = rig.sender.find("No imaging tonight")
    assert level == "warning" and text == "Roof not safe."
    assert rig.sender.titles().count("No imaging tonight") == 1


def test_dawn_after_a_session_with_no_frames_says_so(rig):
    rig.settle_and_start()
    rig.sun = -10.0
    rig.tick(10)
    assert "nothing was stacked" in rig.sender.find("No imaging tonight")[1]


def test_dawn_after_imaging_sums_up_the_night_across_sessions(rig):
    rig.settle_and_start()
    rig.stack("A", 2, 120)
    rig.sun = -10.0
    rig.tick(10)
    title, text, level = rig.sender.find("Imaging finished: dawn")
    assert "A: 120 frames, 1 h 0 min" in text
    assert "No imaging tonight" not in rig.sender.titles()
    assert "A: 120 frames, 1 h 0 min" in rig.sender.find("Night summary")[1]
    assert "Total: 1 h 0 min" in rig.sender.find("Night summary")[1]


def test_a_session_started_in_the_app_counts_as_imaging(rig):
    rig.safety = UNSAFE
    rig.set_scope(view_state="working", view_stage="Stack", stacked=5, view_target="Z")
    rig.tick()
    rig.sun = -10.0
    rig.tick(10)
    assert "No imaging tonight" not in rig.sender.titles()


def test_a_restart_does_not_repeat_or_forget_the_night(rig):
    _wait_in_the_dark(rig, roof_alerts.WAIT_ALERT_S + 1)
    assert rig.sender.titles() == ["Not imaging yet"]
    rig.auto = rig.build()  # ALP restarts in the dark
    rig.tick()
    rig.now += roof_alerts.WAIT_ALERT_S + 1
    rig.tick()
    assert "Not imaging yet" not in rig.sender.titles()
    rig.sun = -10.0
    rig.tick(10)
    assert "No imaging tonight" in rig.sender.titles()
    rig.auto = rig.build()  # and again, after dawn
    rig.tick(10)
    assert rig.sender.sent == []


def test_a_new_night_starts_fresh(rig):
    _wait_in_the_dark(rig, roof_alerts.WAIT_ALERT_S + 1)
    rig.sun = -10.0
    rig.tick(10)
    rig.sender.sent.clear()
    rig.now += 86400
    rig.tick()
    rig.sun = -40.0
    rig.tick(10)
    rig.now += roof_alerts.WAIT_ALERT_S + 1
    rig.tick()
    assert "Not imaging yet" in rig.sender.titles()


# ---- safety --------------------------------------------------------------------


class ExplodingAlerts:
    def __getattr__(self, name):
        def explode(*a, **k):
            raise RuntimeError("alerts are broken")

        return explode


def test_broken_alerts_cannot_stop_the_park(tmp_path):
    rig = Rig(tmp_path)
    rig.auto._alerts = ExplodingAlerts()
    rig.settle_and_start()
    rig.safety = UNSAFE
    rig.tick()
    rig.tick(31)
    assert "park" in rig.stop_calls()


def test_the_loop_runs_without_alerts_configured(tmp_path):
    rig = Rig(tmp_path)
    assert isinstance(rig.auto, RoofAutomation) and rig.auto._alerts is None
    rig.settle_and_start()


def test_state_file_is_plain_json_with_no_webhook(rig):
    rig.settle_and_start()
    doc = json.loads(open(rig.state_path + ".alerts").read())
    assert doc["sessions"] == 1 and "webhook" not in json.dumps(doc)


# ---- which alerts post -----------------------------------------------------------


def test_muted_kinds_stay_quiet_but_alarms_always_post(tmp_path):
    sender = RecordingSender()
    alerts = NightAlerts(sender, muted=lambda: tuple(roof_alerts.KINDS))
    alerts.parked()
    alerts.session_started("Plan", ["A"], 1, False, "n")
    alerts.alarm("PARK NOT CONFIRMED")
    assert sender.titles() == ["Seestar alarm"]


def test_muting_passes_but_not_failures_is_possible(rig):
    rig.alerts._muted = lambda: ("autofocus_passed",)
    log = focus.FocusLog()
    log.on_finish = rig.alerts.autofocus
    rig.settle_and_start()
    _focus_events(log, {"state": "complete"})
    _focus_events(log, {"state": "fail", "error": "no star"})
    titles = [t for t in rig.sender.titles() if t.startswith("Autofocus")]
    assert [t.split(" on ")[0] for t in titles] == ["Autofocus failed"]


# ---- saving the webhook ----------------------------------------------------------


def test_only_a_real_discord_webhook_is_accepted():
    assert roof_alerts.parse_webhook(f"  {URL}\n") == URL
    canary = "https://canary.discord.com/api/webhooks/1/abc-_D"
    assert roof_alerts.parse_webhook(canary) == canary
    for bad in (
        "",
        "http://discord.com/api/webhooks/1/abc",
        "https://evil.example/api/webhooks/1/abc",
        "https://discord.com.evil.example/api/webhooks/1/abc",
        "https://discord.com/api/webhooks/abc/def",
    ):
        assert roof_alerts.parse_webhook(bad) == "", bad


def test_pasting_discords_webhook_json_works():
    pasted = json.dumps({"id": "123456", "name": "Seestar", "token": "x", "url": URL}, indent=1)
    assert roof_alerts.parse_webhook(pasted) == URL


def test_the_webhook_file_is_private(tmp_path):
    path = tmp_path / "hook.txt"
    roof_alerts.write_webhook(str(path), URL)
    assert path.stat().st_mode & 0o777 == 0o600
    assert roof_alerts.read_webhook(str(path)) == URL
    roof_alerts.remove_webhook(str(path))
    roof_alerts.remove_webhook(str(path))  # already gone is fine
    assert roof_alerts.read_webhook(str(path)) == ""


def test_one_try_when_a_person_is_waiting():
    calls, waits = [], []

    def post(*a, **k):
        calls.append(a)
        raise requests.ConnectionError("down")

    sent, _ = deliver(URL, {}, post=post, sleep=waits.append, tries=1)
    assert not sent and len(calls) == 1 and waits == []


def test_delivery_result_is_kept_for_the_page(monkeypatch):
    monkeypatch.setattr(roof_alerts, "_last_delivery", None)
    sender = DiscordSender(lambda: URL, post=lambda *a, **k: _reply(404), sleep=lambda s: None)
    sender._queue.append((URL, roof_alerts.payload("Arm parked", "x", "info", 0)))
    sender.drain()
    assert roof_alerts._last_delivery["title"] == "Arm parked"
    assert roof_alerts._last_delivery["sent"] is False
    assert roof_alerts._last_delivery["status"] == "HTTP 404"


def test_page_settings_round_trip_through_real_toml(monkeypatch):
    import tomlkit

    from device.config import Config
    from roof import config as roof_config

    monkeypatch.setattr(Config, "_dict", tomlkit.document())
    monkeypatch.setattr(Config, "save_toml", lambda *a, **k: None)
    assert roof_config.load().discord_muted == ()
    roof_config.save("discord_enabled", True)
    roof_config.save("discord_muted", ["parked", "stacking"])
    loaded = roof_config.load()
    assert loaded.discord_enabled is True
    assert loaded.discord_muted == ("parked", "stacking")
    roof_config.save("discord_muted", [])
    assert roof_config.load().discord_muted == ()
