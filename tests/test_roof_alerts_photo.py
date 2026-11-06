"""The Stacked image alert: the scope's saved stack, posted once it appears."""

import json
import os
from types import SimpleNamespace

import pytest

from roof import alerts as roof_alerts

URL = "https://discord.com/api/webhooks/1/secret"


def _saved(folder, name, when):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"jpeg")
    os.utime(path, (when, when))
    return str(path)


def test_finds_the_newest_stack_of_this_run(tmp_path):
    _saved(tmp_path / "M 42", "Stacked_5_M 42_30.0s_LP_old.jpg", 500)
    newest = _saved(tmp_path / "M 42", "Stacked_9_M 42_30.0s_LP_new.jpg", 1000)
    _saved(tmp_path / "M 42", "Stacked_9_M 42_30.0s_LP_new_thn.jpg", 2000)
    assert roof_alerts.find_stack(str(tmp_path), "M 42", 900) == newest


def test_a_mosaic_is_saved_under_its_own_folder(tmp_path):
    path = _saved(tmp_path / "IC 1805_mosaic", "Stacked_8_mosaic_IC 1805_20.0s.jpg", 1000)
    assert roof_alerts.find_stack(str(tmp_path), "IC 1805", 900) == path


def test_an_older_stack_is_never_sent_as_tonights(tmp_path):
    _saved(tmp_path / "M 42", "Stacked_5_M 42.jpg", 100)
    assert roof_alerts.find_stack(str(tmp_path), "M 42", 1000) is None


@pytest.mark.parametrize("name", ["", "../secrets", "a/b"])
def test_a_strange_name_finds_nothing(tmp_path, name):
    assert roof_alerts.find_stack(str(tmp_path), name, 0) is None


def test_a_missing_share_finds_nothing(tmp_path):
    assert roof_alerts.find_stack(str(tmp_path / "unmounted"), "M 42", 0) is None


class Reply:
    status_code = 204


def _sender(tmp_path, posts, clock=None):
    return roof_alerts.DiscordSender(
        lambda: URL,
        clock=clock or (lambda: 1000.0),
        post=lambda url, **kwargs: posts.append(kwargs) or Reply(),
        sleep=lambda s: None,
    )


def test_the_image_is_uploaded_with_the_message(tmp_path):
    path = _saved(tmp_path / "M 42", "Stacked_9.jpg", 1000)
    posts = []
    sender = _sender(tmp_path, posts)
    sender._post_when_saved("Stacked: M 42", "9 frames.", lambda: path)
    sender.drain()
    body = json.loads(posts[0]["data"]["payload_json"])
    assert body["embeds"][0]["image"]["url"] == "attachment://stack.jpg"
    assert body["allowed_mentions"] == {"parse": []}
    assert posts[0]["files"]["files[0]"][1] == b"jpeg"


def test_it_waits_for_the_scope_to_save_it(tmp_path):
    path = _saved(tmp_path / "M 42", "Stacked_9.jpg", 1000)
    answers = [None, None, path]
    posts = []
    sender = _sender(tmp_path, posts)
    sender._post_when_saved("T", "x", lambda: answers.pop(0))
    sender.drain()
    assert len(posts) == 1


def test_no_image_means_no_message_and_it_gives_up(tmp_path):
    now = [1000.0]

    def clock():
        now[0] += 60
        return now[0]

    posts = []
    sender = _sender(tmp_path, posts, clock=clock)
    sender._post_when_saved("T", "x", lambda: None)
    sender.drain()
    assert posts == []


def test_a_bad_file_never_raises(tmp_path):
    posts = []
    sender = _sender(tmp_path, posts)
    sender._post_when_saved("T", "x", lambda: str(tmp_path / "gone.jpg"))
    sender.drain()
    assert posts == []


def test_a_very_large_stack_is_shrunk_to_fit(tmp_path, monkeypatch):
    import cv2
    import numpy as np

    image = np.random.randint(0, 255, (3840, 2160, 3), dtype=np.uint8)
    path = tmp_path / "big.jpg"
    cv2.imwrite(str(path), image, [cv2.IMWRITE_JPEG_QUALITY, 100])
    monkeypatch.setattr(roof_alerts, "PHOTO_MAX_BYTES", 1000)
    monkeypatch.setattr(roof_alerts, "PHOTO_MAX_SIDE", 400)
    decoded = cv2.imdecode(np.frombuffer(roof_alerts.photo_bytes(str(path)), np.uint8), cv2.IMREAD_COLOR)
    assert max(decoded.shape[:2]) == 400


class FakeSender:
    def __init__(self):
        self.photos = []

    def send(self, title, text, level="info"):
        pass

    def send_photo(self, title, text, find):
        self.photos.append((title, text, find))


def _alerts(tmp_path, muted=()):
    sender = FakeSender()
    alerts = roof_alerts.NightAlerts(
        sender, clock=lambda: 1000.0, muted=lambda: muted, image_dir=lambda: str(tmp_path)
    )
    alerts._session = {"targets": [], "adopted": False}
    return alerts, sender


def _with_frames(alerts, frames=9):
    alerts._current = roof_alerts._Target("M 42", started=900.0, frames=frames)


def test_a_finished_target_sends_its_image(tmp_path):
    alerts, sender = _alerts(tmp_path)
    _with_frames(alerts)
    alerts._close_target()
    title, text, find = sender.photos[0]
    assert title == "Stacked: M 42" and "9 frames" in text
    path = _saved(tmp_path / "M 42", "Stacked_9.jpg", 1000)
    assert find() == path


def test_a_target_with_no_frames_sends_nothing(tmp_path):
    alerts, sender = _alerts(tmp_path)
    _with_frames(alerts, frames=0)
    alerts._close_target()
    assert sender.photos == []


def test_it_can_be_switched_off(tmp_path):
    alerts, sender = _alerts(tmp_path, muted=("stack_image",))
    _with_frames(alerts)
    alerts._close_target()
    assert sender.photos == []


def test_a_dry_run_sends_no_image(tmp_path):
    alerts, sender = _alerts(tmp_path)
    alerts._dry_run = True
    _with_frames(alerts)
    alerts._close_target()
    assert sender.photos == []


def test_the_switch_is_on_the_automation_page():
    assert "stack_image" in roof_alerts.KINDS
    assert "stack_image" in {k["key"] for k in roof_alerts.panel(SimpleNamespace(discord_enabled=True, discord_muted=()))["kinds"]}
