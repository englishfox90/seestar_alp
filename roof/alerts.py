"""Discord alerts for what the scope does through the night.

Covers imaging only. PFR Sentinel already posts the roof opening and closing,
weather and all-sky images, so nothing here announces those; a roof closing only
appears as the reason a session ended.

DiscordSender never blocks: the automation loop owns every safety decision, so a
slow or dead network must not hold it up. Messages wait on a bounded queue and
are posted from their own thread, oldest dropped first.

The webhook is a secret, and support bundles include config.toml, so it lives in
its own file (see WEBHOOK_FILE) rather than in the [roof] section. It is never
sent back to a browser: the page only shows that one is saved.
"""

import argparse
import collections
import glob
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from device.config import Config
from roof import focus

WEBHOOK_FILE = "discord_webhook.txt"
USERNAME = "Seestar"
QUEUE_MAX = 200
BACKOFF_S = (1, 4, 16)
MAX_RETRY_AFTER_S = 60
POST_TIMEOUT_S = 10
UPLOAD_TIMEOUT_S = 60
# The scope writes the stack's JPG as the stack stops, and the share can lag behind that.
PHOTO_WAIT_S = 240
PHOTO_POLL_S = 15
# How much earlier than the target's start a file's clock may read and still count.
PHOTO_CLOCK_SLACK_S = 120
# Discord's smallest upload limit is 10 MB. A larger stack is shrunk to fit.
PHOTO_MAX_BYTES = 8 * 1024 * 1024
PHOTO_MAX_SIDE = 2160
PHOTO_NAME = "stack.jpg"
TITLE_MAX, TEXT_MAX = 256, 4096

WAIT_ALERT_S = 1800  # how long into the dark before saying imaging hasn't started
LOST_CONTACT_S = 120
REPEAT_ALARM_S = 900

# What the Automation page's switches control. Alarms and lost contact aren't here:
# they always post, because they're how you hear that something needs you.
KINDS = {
    "session": ("Session started and ended", "With the reason, and frames per target"),
    "autofocus_passed": ("Autofocus passed", ""),
    "autofocus_failed": ("Autofocus failed", "Includes a run with no usable focus curve"),
    "stacking": ("Stacking started", "When each target's first frame is stacked"),
    "switching": ("Switching target", "With the last target's frames"),
    "stack_image": ("Stacked image", "A copy of the final stack when a target finishes"),
    "parked": ("Arm parked", ""),
    "night": ("Night status", "Not imaging yet, no imaging tonight, and the night summary"),
}
ALWAYS = "always"

_COLOURS = {"info": 0x0EA5E9, "success": 0x22C55E, "warning": 0xF59E0B, "error": 0xEF4444}
_WEBHOOK_PATH = re.compile(r"/api/webhooks/\d+/[^\s/)'\"]+", re.I)
_WEBHOOK_URL = re.compile(
    r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+"
)
_last_delivery = None


def redact(text) -> str:
    """Remove the webhook from text bound for a log: its token is a live secret."""
    return _WEBHOOK_PATH.sub("/api/webhooks/[REDACTED]", str(text))


def webhook_path() -> str:
    return os.path.join(os.path.dirname(Config.path_to_dat), WEBHOOK_FILE)


def read_webhook(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def parse_webhook(text: str) -> str:
    """The webhook in what was pasted, or "".

    Discord's page also offers the webhook as JSON, so that works too. Only a real
    Discord address is accepted, because the server posts to whatever is saved.
    """
    found = _WEBHOOK_URL.search(text or "")
    return found.group(0) if found else ""


def write_webhook(path: str, url: str) -> None:
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(url + "\n")
    os.replace(tmp, path)


def remove_webhook(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def send_test(url: str) -> tuple[bool, str]:
    """One try, so a person pressing the button isn't left waiting."""
    body = payload("Test alert", "Seestar ALP can reach this channel.", "info", time.time())
    return deliver(url, body, tries=1)


def panel(settings) -> dict:
    """What the Automation page shows. Never includes the webhook itself."""
    url = read_webhook(webhook_path())
    return {
        "enabled": settings.discord_enabled,
        "saved": bool(url),
        "ends_with": url.split("/api/webhooks/")[-1].split("/")[0][-4:] if url else "",
        "kinds": [
            {
                "key": key,
                "label": label,
                "help": help_text,
                "on": key not in settings.discord_muted,
            }
            for key, (label, help_text) in KINDS.items()
        ],
        "last": _last_delivery,
    }


def payload(title: str, text: str, level: str, when: float) -> dict:
    return {
        "username": USERNAME,
        # A target name must never be able to ping anyone.
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": title[:TITLE_MAX],
                "description": text[:TEXT_MAX],
                "color": _COLOURS.get(level, _COLOURS["info"]),
                "timestamp": datetime.fromtimestamp(when, timezone.utc).isoformat(),
            }
        ],
    }


def find_stack(directory: str, name: str, since: float) -> str | None:
    """The newest saved stack of a target, if it's from this run. None until it appears.

    The scope saves single frames under the target's name, and mosaics with _mosaic added.
    """
    if not directory or not name or os.path.basename(name) != name:
        return None
    found = []
    for folder in (name, f"{name}_mosaic"):
        for path in glob.glob(os.path.join(glob.escape(directory), glob.escape(folder), "Stacked_*.jpg")):
            if path.endswith("_thn.jpg"):
                continue
            try:
                found.append((os.path.getmtime(path), path))
            except OSError:
                continue
    found = [item for item in found if item[0] >= since - PHOTO_CLOCK_SLACK_S]
    return max(found)[1] if found else None


def photo_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        data = f.read()
    if len(data) <= PHOTO_MAX_BYTES:
        return data
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("the stack could not be read")
    height, width = image.shape[:2]
    shrink = PHOTO_MAX_SIDE / max(height, width)
    if shrink < 1:
        image = cv2.resize(image, (round(width * shrink), round(height * shrink)), interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise ValueError("the stack could not be shrunk")
    return encoded.tobytes()


def deliver(
    url, body, post=requests.post, sleep=time.sleep, logger=None, tries=len(BACKOFF_S), photo=None
) -> tuple[bool, str]:
    """Post one message, waiting out rate limits. Returns (sent, what happened)."""
    logger = logger or logging.getLogger("roof")
    status = "not attempted"
    for attempt, backoff in enumerate(BACKOFF_S[:tries]):
        try:
            if photo is None:
                reply = post(url, json=body, timeout=POST_TIMEOUT_S)
            else:
                reply = post(
                    url,
                    data={"payload_json": json.dumps(body)},
                    files={"files[0]": (PHOTO_NAME, photo, "image/jpeg")},
                    timeout=UPLOAD_TIMEOUT_S,
                )
        except requests.RequestException as e:
            status = f"{type(e).__name__}: {redact(e)}"
        else:
            if reply.status_code < 300:
                return True, f"HTTP {reply.status_code}"
            status = f"HTTP {reply.status_code}"
            if reply.status_code != 429:
                return False, status  # a bad webhook won't improve with retries
            try:
                backoff = min(float(reply.json()["retry_after"]), MAX_RETRY_AFTER_S)
            except (ValueError, KeyError, TypeError):
                pass
        if attempt < tries - 1:
            logger.info("Discord alert not sent (%s); retrying in %gs", status, backoff)
            sleep(backoff)
    return False, status


class DiscordSender:
    def __init__(self, get_url, clock=time.time, post=requests.post, sleep=time.sleep, logger=None):
        self._get_url = get_url  # "" means off; read per message so edits apply at once
        self._clock = clock
        self._post = post
        self._sleep = sleep
        self._logger = logger or logging.getLogger("roof")
        self._queue = collections.deque(maxlen=QUEUE_MAX)
        self._wake = threading.Event()
        self._thread = None
        self._thread_lock = threading.Lock()

    def send(self, title: str, text: str, level: str = "info") -> None:
        """Queue a message and return straight away."""
        url = self._get_url()
        if not url:
            return
        self._enqueue(url, payload(title, text, level, self._clock()))

    def send_photo(self, title: str, text: str, find) -> None:
        """Post a saved image once ``find()`` returns its path. Returns straight away.

        It waits on a thread of its own, so an alarm queued meanwhile isn't held up, and
        the wait can't touch the automation loop. No image means no message: the target's
        frames have already been reported.
        """
        if not self._get_url():
            return
        threading.Thread(
            target=self._post_when_saved, args=(title, text, find), daemon=True, name="RoofAlertPhoto"
        ).start()

    def _post_when_saved(self, title, text, find):
        try:
            deadline = self._clock() + PHOTO_WAIT_S
            path = find()
            while path is None and self._clock() < deadline:
                self._sleep(PHOTO_POLL_S)
                path = find()
            if path is None:
                self._logger.info("Discord: no saved stack appeared for %s", title)
                return
            url = self._get_url()
            if not url:
                return
            body = payload(title, text, "success", self._clock())
            body["embeds"][0]["image"] = {"url": f"attachment://{PHOTO_NAME}"}
            self._enqueue(url, body, photo_bytes(path))
        except Exception as e:
            self._logger.warning("Discord: could not prepare the stack image: %s", redact(e))

    def _enqueue(self, url, body, photo=None):
        self._queue.append((url, body, photo))
        self._wake.set()
        with self._thread_lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True, name="RoofAlerts")
                self._thread.start()

    def _run(self):
        while True:
            self._wake.wait()
            self._wake.clear()
            self.drain()

    def drain(self) -> None:
        while True:
            try:
                url, body, photo = self._queue.popleft()
            except IndexError:
                return
            try:
                sent, status = deliver(url, body, self._post, self._sleep, self._logger, photo=photo)
            except Exception as e:
                sent, status = False, redact(e)
            title = body["embeds"][0]["title"]
            global _last_delivery
            _last_delivery = {
                "title": title,
                "sent": sent,
                "status": status,
                "at": datetime.fromtimestamp(self._clock()).strftime("%H:%M"),
            }
            if not sent:
                self._logger.warning("Discord alert dropped (%s): %s", status, title)


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


def _totals(frames, dropped, seconds) -> str:
    if not frames:
        return "no frames"
    parts = [f"{frames} frames", _duration(seconds)]
    if dropped:
        parts.append(f"{dropped} dropped")
    return ", ".join(parts)


@dataclass
class _Target:
    name: str
    started: float = 0.0
    frames: int = 0
    dropped: int = 0
    seconds: float = 0.0
    last_stacked: int = 0
    last_dropped: int = 0
    announced: bool = False
    focus_doubt: str = ""
    skip_reason: str = ""

    def note_stack(self, stacked, dropped, exposure_s):
        # Each mosaic panel restarts the firmware's count.
        new = stacked - self.last_stacked if stacked >= self.last_stacked else stacked
        lost = dropped - self.last_dropped if dropped >= self.last_dropped else dropped
        self.frames += new
        self.dropped += lost
        self.seconds += new * exposure_s
        self.last_stacked, self.last_dropped = stacked, dropped

    def summary(self) -> str:
        return _totals(self.frames, self.dropped, self.seconds)


# reason -> (title, level, what happens next)
_ENDINGS = {
    "roof unsafe": (
        "Imaging stopped: roof unsafe",
        "warning",
        "Imaging can restart if the roof reads safe again before dawn.",
    ),
    "dawn": ("Imaging finished: dawn", "info", ""),
    "manual": (
        "Imaging stopped by Stop + Park",
        "warning",
        "Holding until Start or the next night.",
    ),
    "stopped elsewhere": (
        "Imaging stopped outside automation",
        "warning",
        "Holding until Start or the next night.",
    ),
    "schedule finished": ("Imaging session finished", "success", ""),
}


class NightAlerts:
    """Turns the automation's ticks and decisions into messages."""

    def __init__(self, sender, state_path=None, clock=time.time, logger=None, muted=None, image_dir=None):
        self._sender = sender
        self._muted = muted or (lambda: ())
        self._image_dir = image_dir or (lambda: "")
        self._state_path = state_path
        self._clock = clock
        self._logger = logger or logging.getLogger("roof")
        self._lock = threading.RLock()  # autofocus arrives on the scope's socket thread
        self._dry_run = False
        self._session = None  # {"targets": [...], "adopted": bool}
        self._current = None
        self._was_dark = None
        self._dark_since = None
        self._last_note = ""
        self._lost_since = None
        self._lost_announced = False
        self._alarms = {}
        self._night = self._blank_night("")
        self._load()

    @staticmethod
    def _blank_night(key) -> dict:
        return {"night": key, "sessions": 0, "imaged": False, "waiting_sent": False,
                "closed_sent": False, "targets": {}}  # fmt: skip

    # ---- what the automation decides ------------------------------------------

    def session_started(self, schedule, names, start_position, resumed, night):
        with self._lock:
            self._roll_night(night)
            self._night["sessions"] += 1
            self._save()
            self._session = {"targets": [], "adopted": False}
            self._current = None
            self._reset_contact()
            lines = [f"Schedule: {schedule}"]
            if names:
                total = start_position + len(names) - 1
                lines.append(f"First up: {names[0]} (target {start_position} of {total})")
            if len(names) > 1:
                lines.append("Then: " + ", ".join(names[1:]))
            title = "Imaging session resumed" if resumed else "Imaging session started"
            self._send(title, "\n".join(lines), "success", "session")

    def session_adopted(self, night):
        with self._lock:
            self._roll_night(night)
            self._night["sessions"] += 1
            self._save()
            self._session = {"targets": [], "adopted": True}
            self._current = None
            self._reset_contact()
            self._send(
                "Imaging session started elsewhere",
                "A schedule not started by the automation is running; watching it.",
                kind="session",
            )

    def session_ended(self, reason, more_tonight=False):
        """Safe to call twice: only the first call for a session says anything."""
        with self._lock:
            if self._session is None:
                return
            self._close_target()
            targets = self._session["targets"]
            self._session = None
            title, level, next_step = _ENDINGS.get(
                reason, (f"Imaging stopped: {reason}", "warning", "")
            )
            lines = [f"{t.name}: {t.summary()}" for t in targets] or ["No target was reached."]
            if level == "success" and not any(t.frames for t in targets):
                level = "warning"
            if more_tonight:
                next_step = "The target list may have more for tonight."
            if next_step:
                lines.append(next_step)
            self._send(title, "\n".join(lines), level, "session")

    def session_forgotten(self):
        with self._lock:
            self._session = None
            self._current = None

    def target_skipped(self, name, why):
        with self._lock:
            if self._current is not None and self._current.name == name:
                self._current.skip_reason = why
            else:
                self._send(f"Skipping {name}", why, "warning", "switching")

    def parked(self):
        self._send("Arm parked", "The firmware reports the mount closed.", kind="parked")

    def alarm(self, message):
        with self._lock:
            now = self._clock()
            if now - self._alarms.get(message, -REPEAT_ALARM_S) < REPEAT_ALARM_S:
                return
            self._alarms = {
                m: at for m, at in self._alarms.items() if now - at < REPEAT_ALARM_S
            }
            self._alarms[message] = now
            self._send("Seestar alarm", message, "error")

    def autofocus(self, run):
        """A FocusLog run ending, on the scope's socket thread."""
        state = run.get("state")
        if state not in ("complete", "fail"):
            return  # stopping a view reports "cancel"; that isn't a focus result
        with self._lock:
            if self._session is None:
                return
            current = self._current
            where = f" on {current.name}" if current else ""
            summary = focus.chart(run)["summary"]
            curve = run.get("curve")
            if state == "fail":
                title, level, doubt = f"Autofocus failed{where}", "error", "autofocus failed"
            elif curve is not None and not focus.fit_is_usable(curve):
                title, level, doubt = (
                    f"Autofocus gave no usable fit{where}",
                    "warning",
                    "autofocus gave no usable fit",
                )
                summary = "The scope finished its run but could not fit a focus curve."
            else:
                title, level, doubt = f"Autofocus passed{where}", "success", ""
            if current:
                current.focus_doubt = doubt
            kind = "autofocus_failed" if doubt else "autofocus_passed"
            self._send(title, summary, level, kind)

    # ---- what each tick shows --------------------------------------------------

    def observe(self, state, dark, night, phase, note, position, dry_run):
        """Call once per tick. ``position`` is (target index, total), or (0, 0)."""
        with self._lock:
            self._dry_run = dry_run
            self._roll_night(night)
            now = self._clock()
            stacking = (
                state.view_state == "working" and state.view_stage == "Stack" and state.stacked > 0
            )
            if stacking and not self._night["imaged"]:
                self._night["imaged"] = True  # also true of a session started elsewhere
                self._save()
            if self._session is not None:
                self._watch_contact(state, now)
                self._follow_target(state, stacking, position)
            self._watch_the_dark(dark, phase, note, now)

    def _watch_contact(self, state, now):
        if state.connected:
            if self._lost_announced:
                self._send("Back in contact with the scope", "Its session is being followed again.")
            self._reset_contact()
            return
        if self._lost_since is None:
            self._lost_since = now
        if not self._lost_announced and now - self._lost_since >= LOST_CONTACT_S:
            self._lost_announced = True
            self._send(
                "Lost contact with the scope",
                f"No reply for {LOST_CONTACT_S // 60} minutes during an imaging session.",
                "error",
            )

    def _reset_contact(self):
        self._lost_since = None
        self._lost_announced = False

    def _follow_target(self, state, stacking, position):
        if not state.connected or state.sched_state != "working":
            return
        index, total = position
        # A settings item between targets still reports the last view's name, so an
        # automation session only trusts the name while on an imaging item.
        if state.target and (index or self._session["adopted"]):
            if self._current is None:
                self._current = _Target(state.target, started=self._clock())
            elif self._current.name != state.target:
                self._switch_to(state.target, index, total)
        current = self._current
        if current is None or not stacking:
            return
        current.note_stack(state.stacked, state.dropped, state.exposure_s)
        if not current.announced:
            current.announced = True
            self._announce_stacking(current, state)

    def _switch_to(self, name, index, total):
        previous = self._current
        self._close_target()
        self._current = _Target(name, started=self._clock())
        where = f" (target {index} of {total})" if index else ""
        lines = [f"Moving to {name}{where}."]
        level = "info"
        if previous.skip_reason:
            lines.append(f"{previous.name} skipped: {previous.skip_reason}. {previous.summary()}.")
        elif previous.frames:
            lines.append(f"{previous.name} finished: {previous.summary()}.")
        else:
            level = "warning"
            lines.append(f"{previous.name} stacked nothing: its slew or stack probably failed.")
        self._send(f"Switching target: {name}", "\n".join(lines), level, "switching")

    def _announce_stacking(self, target, state):
        details = []
        if state.exposure_s:
            details.append(f"{state.exposure_s:g} s exposures")
        if state.gain is not None:
            details.append(f"gain {state.gain}")
        if state.mosaic_scale and state.mosaic_scale > 1:
            details.append(f"framed at {state.mosaic_scale:g}x")
        text = ", ".join(details) or "First frame stacked."
        level = "success"
        if target.focus_doubt:
            text += f"\nStacking anyway: {target.focus_doubt}, so focus isn't confirmed."
            level = "warning"
        self._send(f"Stacking {target.name}", text, level, "stacking")

    def _close_target(self):
        target, self._current = self._current, None
        if target is None:
            return
        self._session["targets"].append(target)
        totals = self._night["targets"].setdefault(
            target.name, {"frames": 0, "dropped": 0, "seconds": 0.0}
        )
        totals["frames"] += target.frames
        totals["dropped"] += target.dropped
        totals["seconds"] += target.seconds
        self._save()
        self._send_stack_image(target)

    def _send_stack_image(self, target):
        directory = self._image_dir()
        if not target.frames or self._dry_run or not directory or "stack_image" in self._muted():
            return
        self._sender.send_photo(
            f"Stacked: {target.name}",
            f"{target.summary()}.",
            lambda: find_stack(directory, target.name, target.started),
        )

    def _watch_the_dark(self, dark, phase, note, now):
        if dark and self._was_dark is not True:
            self._dark_since = now
        if dark and phase == "idle" and note:
            self._last_note = note
        night = self._night
        if (
            dark
            and phase == "idle"
            and note
            and not night["imaged"]
            and not night["sessions"]
            and not night["waiting_sent"]
            and now - self._dark_since >= WAIT_ALERT_S
        ):
            night["waiting_sent"] = True
            self._save()
            self._send("Not imaging yet", self._why(note), kind="night")
        if self._was_dark is True and not dark:
            self._close_the_night()
        self._was_dark = dark

    def _close_the_night(self):
        night = self._night
        if night["closed_sent"]:
            return
        night["closed_sent"] = True
        self._save()
        if night["imaged"]:
            lines = [
                f"{name}: {_totals(t['frames'], t['dropped'], t['seconds'])}"
                for name, t in night["targets"].items()
            ]
            total = sum(t["seconds"] for t in night["targets"].values())
            lines.append(f"Total: {_duration(total)}")
            self._send("Night summary", "\n".join(lines), "success", "night")
        elif night["sessions"]:
            self._send(
                "No imaging tonight",
                "A session started, but nothing was stacked before dawn.",
                "warning",
                "night",
            )
        else:
            self._send(
                "No imaging tonight",
                self._why(self._last_note) if self._last_note else "It never started.",
                "warning",
                "night",
            )

    @staticmethod
    def _why(note) -> str:
        text = note.removeprefix("waiting: ")
        return text[:1].upper() + text[1:] + "."

    def _roll_night(self, key):
        if self._night["night"] != key:
            self._night = self._blank_night(key)
            self._last_note = ""
            self._save()

    # ---- plumbing ---------------------------------------------------------------

    def _send(self, title, text, level="info", kind=ALWAYS):
        if kind in self._muted():
            return
        prefix = "[DRY RUN] " if self._dry_run else ""
        self._sender.send(prefix + title, text, level)

    def _load(self):
        if not self._state_path:
            return
        try:
            with open(self._state_path) as f:
                doc = json.load(f)
        except (OSError, ValueError):
            return
        if isinstance(doc, dict) and isinstance(doc.get("targets"), dict):
            self._night = {**self._blank_night(""), **doc}

    def _save(self):
        if not self._state_path:
            return
        tmp = f"{self._state_path}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self._night, f)
            os.replace(tmp, self._state_path)
        except OSError as e:
            self._logger.warning("could not save the alert state: %s", e)


def main() -> int:
    """Post a test message: python -m roof.alerts --test"""
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--test", action="store_true", help="post a test message and report")
    args = parser.parse_args()
    path = webhook_path()
    url = read_webhook(path)
    if not url:
        print(f"No webhook in {path}")
        return 1
    if not args.test:
        parser.print_help()
        return 0
    sent, status = send_test(url)
    print(f"{'Sent' if sent else 'Not sent'}: {status}")
    return 0 if sent else 1


if __name__ == "__main__":
    raise SystemExit(main())
