"""Discord alerts for what the scope does through the night.

Covers imaging only. The safety monitor's own alerts (PFR Sentinel's, say) post the roof opening and closing,
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
# Health warnings. Thresholds the page doesn't set.
BATTERY_REARM_MARGIN = 10  # percent above the limit before a low battery can warn again
HEALTH_FRESH_S = 600  # a footer only shows readings this recent
DEW_WARN_S = 15 * 60
BRIEFING_LATE_S = 3600  # after dark, a briefing this late is still worth posting
NIGHT_ALARMS_KEPT = 20
FOLDER_CHECK_S = 300  # how often to look whether the MyWorks folder is connected
FIELDS_MAX = 25  # Discord's limit per embed
LOST_CONTACT_S = 120
REPEAT_ALARM_S = 900

# What the Automation page's switches control. Alarms and lost contact aren't here:
# they always post, because they're how you hear that something needs you.
KINDS = {
    "briefing": ("Tonight's briefing", "Before dark: roof forecast, moon, the plan and the scope's health"),
    "session": ("Session started and ended", "With the reason, and frames per target"),
    "autofocus_passed": ("Autofocus passed", ""),
    "autofocus_failed": ("Autofocus failed", "Includes a run with no usable focus curve"),
    "stacking": ("Stacking started", "When each target's first frame is stacked"),
    "switching": ("Switching target", "With the last target's frames"),
    "stack_image": ("Stacked image", "A copy of the final stack when a target finishes"),
    "parked": ("Arm parked", ""),
    "night": ("Night report", "Not imaging yet, and after dawn the night's report with its stacks"),
    "battery": ("Battery and power", "Battery low while not charging, or charging stops"),
    "temperature": ("Overheating", "When the scope or its battery reports overheating"),
    "storage": ("Storage low", "When the scope's free space falls below your limit"),
    "dew": ("Dew point reached", "Air at the dew point for 15 minutes with the heater on"),
    "stacks_folder": (
        "Stacks folder not connected",
        "The scope's MyWorks folder is missing or empty, so stack pictures and framing checks wait",
    ),
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


def health_text(health) -> str:
    """The scope's health in one line, for a footer: battery, charging, temperature, space."""
    if not health:
        return ""
    parts = []
    if health.get("battery") is not None:
        battery = f"Battery {health['battery']}%"
        if health.get("charging") is True:
            battery += ", charging" if health.get("charger") != "Full" else ", on power"
        elif health.get("charging") is False:
            battery += ", not charging"
        parts.append(battery)
    if health.get("temp_c") is not None:
        parts.append(f"{health['temp_c']:.0f} °C")
    if health.get("free_gb") is not None:
        parts.append(f"{health['free_gb']:.0f} GB free")
    return " · ".join(parts)


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
        "briefing_lead_min": f"{getattr(settings, 'briefing_lead_min', 60):g}",
        "battery_pct": f"{getattr(settings, 'battery_alert_pct', 20):g}",
        "storage_gb": f"{getattr(settings, 'storage_alert_gb', 5):g}",
    }


def payload(title: str, text: str, level: str, when: float, fields=None, footer=None, image_name=None) -> dict:
    embed = {
        "title": title[:TITLE_MAX],
        "description": text[:TEXT_MAX],
        "color": _COLOURS.get(level, _COLOURS["info"]),
        "timestamp": datetime.fromtimestamp(when, timezone.utc).isoformat(),
    }
    if fields:
        embed["fields"] = [
            {"name": str(f["name"])[:256], "value": str(f["value"])[:1024], "inline": bool(f.get("inline"))}
            for f in fields[:FIELDS_MAX]
            if f and f.get("value")
        ]
    if footer:
        embed["footer"] = {"text": footer[:2048]}
    if image_name:
        embed["image"] = {"url": f"attachment://{image_name}"}
    return {
        "username": USERNAME,
        # A target name must never be able to ping anyone.
        "allowed_mentions": {"parse": []},
        "embeds": [embed],
    }


def folder_state(directory: str) -> str:
    """Whether the scope's MyWorks folder can be read: "off" when none is set,
    "missing" when it isn't there, "empty" when it's there with nothing in it, else "ok".

    The folder is the scope's share mounted on this Mac. Unmounted, its path is gone or
    an empty mount point, and either means the share isn't connected: on 2026-11-18 it
    was gone all night, and every stack picture and framing check quietly went without.
    """
    if not directory:
        return "off"
    try:
        names = os.listdir(os.path.expanduser(directory))
    except OSError:
        return "missing"
    return "ok" if any(not name.startswith(".") for name in names) else "empty"


class FolderWatch:
    """The MyWorks folder's state, checked on a thread of its own at most every
    ``every_s``, so a share that stops answering can't hold up the automation loop.

    ``state()`` returns at once with the last answer: "unknown" until the first check.
    """

    def __init__(self, directory, every_s=FOLDER_CHECK_S, clock=time.time, check=folder_state, run=None):
        self._directory = directory  # -> the configured folder, or ""
        self._every_s = every_s
        self._clock = clock
        self._check = check
        self._run = run or (
            lambda fn: threading.Thread(target=fn, name="StacksFolderCheck", daemon=True).start()
        )
        self._lock = threading.Lock()
        self._state = "unknown"
        self._checked_for = None
        self._checked_at = None
        self._checking = False

    def state(self) -> str:
        directory = self._directory()
        now = self._clock()
        with self._lock:
            if directory != self._checked_for:
                self._state = "off" if not directory else "unknown"
                self._checked_for, self._checked_at = directory, None
            due = self._checked_at is None or now - self._checked_at >= self._every_s
            if directory and due and not self._checking:
                self._checking = True
                self._run(lambda: self._refresh(directory))
            return self._state

    def _refresh(self, directory):
        try:
            state = self._check(directory)
        except Exception:
            state = "missing"
        with self._lock:
            self._checking = False
            if directory == self._checked_for:
                self._state, self._checked_at = state, self._clock()


def find_stack(directory: str, name: str, since: float) -> str | None:
    """The newest saved stack of a target, if it's from this run. None until it appears.

    The scope saves single frames under the target's name, and mosaics with _mosaic added.
    """
    if not directory or not name or os.path.basename(name) != name:
        return None
    directory = os.path.expanduser(directory)
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
                name, data, mime = photo if isinstance(photo, tuple) else (PHOTO_NAME, photo, "image/jpeg")
                reply = post(
                    url,
                    data={"payload_json": json.dumps(body)},
                    files={"files[0]": (name, data, mime)},
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

    def send(self, title: str, text: str, level: str = "info", fields=None, footer=None, image=None) -> None:
        """Queue a message and return straight away. ``image`` is (name, bytes, mime)."""
        url = self._get_url()
        if not url:
            return
        body = payload(title, text, level, self._clock(), fields, footer, image[0] if image else None)
        self._enqueue(url, body, image)

    def send_built(self, build, label="message") -> None:
        """Work out a message on a thread of its own, then queue it. Returns straight
        away, so a slow forecast or plan can't hold up the automation loop.
        ``build()`` returns send()'s keyword arguments, or None to send nothing."""
        if not self._get_url():
            return

        def run():
            try:
                message = build()
                if message:
                    self.send(**message)
            except Exception as e:
                self._logger.warning("Discord: could not prepare the %s: %s", label, redact(e))

        threading.Thread(target=run, daemon=True, name="RoofAlertBuild").start()

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

    def card(self) -> str:
        """The target's totals for a card field, one fact a line."""
        if self.skip_reason:
            return f"Skipped: {self.skip_reason}\n{self.summary()}"
        if not self.frames:
            return "No frames"
        lines = [f"{self.frames} frames", _duration(self.seconds)]
        if self.dropped:
            lines.append(f"{self.dropped} dropped")
        return "\n".join(lines)


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

    def __init__(
        self,
        sender,
        state_path=None,
        clock=time.time,
        logger=None,
        muted=None,
        image_dir=None,
        briefing=None,
        report=None,
        goal_for=None,
        limits=None,
        folder=None,
    ):
        self._sender = sender
        self._muted = muted or (lambda: ())
        self._image_dir = image_dir or (lambda: "")
        # -> the MyWorks folder's state; see FolderWatch. Checked off this thread.
        self._folder = folder or FolderWatch(self._image_dir)
        self._folder_sent = False
        # Builders for the two long posts: (night, now, health) -> message or None, and
        # (night state, now, health) -> message or None. See roof.night_brief.
        self._briefing = briefing
        self._report = report
        self._goal_for = goal_for or (lambda name: None)  # target name -> "3.1 of 8 h", or None
        # -> {"battery_pct": .., "storage_gb": .., "briefing_lead_min": ..}
        self._limits = limits or (lambda: {})
        self._health = None
        self._health_at = None
        self._battery_low_sent = False
        self._was_charging = None
        self._hot_sent = False
        self._storage_sent = False
        self._dew_since = None
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
                "closed_sent": False, "targets": {}, "briefed": False, "forecast": None,
                "alarms": [], "stacks": [], "dew_sent": False, "last_note": ""}  # fmt: skip

    # ---- what the automation decides ------------------------------------------

    def session_started(self, schedule, names, start_position, resumed, night):
        with self._lock:
            self._roll_night(night)
            self._night["sessions"] += 1
            self._save()
            self._session = {"targets": [], "adopted": False}
            self._current = None
            self._reset_contact()
            fields = [{"name": "Schedule", "value": schedule, "inline": True}]
            if names:
                total = start_position + len(names) - 1
                fields.append({"name": "First up", "value": f"{names[0]} (target {start_position} of {total})", "inline": True})
                goal = self._goal_for(names[0])
                if goal:
                    fields.append({"name": "Goal", "value": goal, "inline": True})
            if len(names) > 1:
                fields.append({"name": "Then", "value": ", ".join(names[1:])})
            title = "Imaging session resumed" if resumed else "Imaging session started"
            self._send(title, "", "success", "session", fields=fields)

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
            fields = [{"name": t.name, "value": t.card(), "inline": True} for t in targets]
            lines = [] if targets else ["No target was reached."]
            if level == "success" and not any(t.frames for t in targets):
                level = "warning"
            if more_tonight:
                next_step = "The target list may have more for tonight."
            if next_step:
                lines.append(next_step)
            self._send(title, "\n".join(lines), level, "session", fields=fields)

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
            self._night["alarms"] = (self._night.get("alarms") or [])[-(NIGHT_ALARMS_KEPT - 1):] + [
                {"at": now, "message": message}
            ]
            self._save()
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

    def observe(self, state, dark, night, phase, note, position, dry_run, dark_start=None):
        """Call once per tick. ``position`` is (target index, total), or (0, 0).
        ``dark_start`` is when tonight's dark begins, for the briefing."""
        with self._lock:
            self._dry_run = dry_run
            self._roll_night(night)
            now = self._clock()
            if state.connected and getattr(state, "health", None):
                self._health, self._health_at = state.health, now
                self._watch_health(state.health)
            self._maybe_brief(night, now, dark_start)
            stacking = (
                state.view_state == "working" and state.view_stage == "Stack" and state.stacked > 0
            )
            if stacking and not self._night["imaged"]:
                self._night["imaged"] = True  # also true of a session started elsewhere
                self._save()
            if self._session is not None:
                self._watch_contact(state, now)
                self._watch_folder()
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
        fields = []
        if state.exposure_s:
            fields.append({"name": "Exposure", "value": f"{state.exposure_s:g} s", "inline": True})
        if state.gain is not None:
            fields.append({"name": "Gain", "value": str(state.gain), "inline": True})
        if state.mosaic_scale and state.mosaic_scale > 1:
            fields.append({"name": "Framing", "value": f"{state.mosaic_scale:g}x", "inline": True})
        goal = self._goal_for(target.name)
        if goal:
            fields.append({"name": "Goal so far", "value": goal, "inline": True})
        text = "" if fields else "First frame stacked."
        level = "success"
        if target.focus_doubt:
            text = f"Stacking anyway: {target.focus_doubt}, so focus isn't confirmed."
            level = "warning"
        self._send(f"Stacking {target.name}", text, level, "stacking", fields=fields)

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
        if target.frames:
            self._night.setdefault("stacks", []).append({"name": target.name, "started": target.started})
        self._save()
        self._send_stack_image(target)

    def _send_stack_image(self, target):
        directory = self._image_dir()
        if not target.frames or self._dry_run or not directory or "stack_image" in self._muted():
            return
        if self._folder.state() in ("missing", "empty"):
            self._logger.info("Discord: no stack picture for %s: the MyWorks folder isn't connected", target.name)
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
            if self._night.get("last_note") != note:
                self._night["last_note"] = note
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
        if self._report is not None:
            if "night" in self._muted():
                return
            state = json.loads(json.dumps(night))
            now, health, prefix = self._clock(), self._fresh_health(), self._prefix()
            self._sender.send_built(
                lambda: self._finish(self._report(state, now, health), prefix), "night report"
            )
            return
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
            self._folder_sent = False
            self._save()

    # ---- plumbing ---------------------------------------------------------------

    def _send(self, title, text, level="info", kind=ALWAYS, fields=None):
        if kind in self._muted():
            return
        footer = health_text(self._fresh_health()) or None
        self._sender.send(self._prefix() + title, text, level, fields=fields, footer=footer)

    def _prefix(self):
        return "[DRY RUN] " if self._dry_run else ""

    def _fresh_health(self):
        if self._health is None or self._health_at is None:
            return None
        return self._health if self._clock() - self._health_at <= HEALTH_FRESH_S else None

    @staticmethod
    def _finish(message, prefix):
        """A builder's message as send()'s keyword arguments, or None."""
        if not message:
            return None
        return {
            "title": prefix + message["title"],
            "text": message.get("text", ""),
            "level": message.get("level", "info"),
            "fields": message.get("fields"),
            "footer": message.get("footer"),
            "image": message.get("image"),
        }

    # ---- the briefing ----------------------------------------------------------

    def _maybe_brief(self, night, now, dark_start):
        if self._briefing is None or dark_start is None or self._night.get("briefed"):
            return
        lead = float(self._limits().get("briefing_lead_min", 60)) * 60
        if not (dark_start - lead <= now <= dark_start + BRIEFING_LATE_S):
            return
        self._night["briefed"] = True
        self._save()
        if "briefing" in self._muted():
            return
        health, prefix = self._fresh_health(), self._prefix()

        def build():
            message = self._briefing(night, now, health)
            if message and message.get("forecast"):
                with self._lock:
                    if self._night.get("night") == night:
                        self._night["forecast"] = message["forecast"]
                        self._save()
            return self._finish(message, prefix)

        self._sender.send_built(build, "briefing")

    # ---- health ------------------------------------------------------------------

    def _watch_health(self, health):
        limits = self._limits()
        battery, charging = health.get("battery"), health.get("charging")
        low = float(limits.get("battery_pct", 20))
        if battery is not None and charging is False and battery <= low and not self._battery_low_sent:
            self._battery_low_sent = True
            self._send(
                "Scope battery low",
                f"Battery at {battery}% and not charging. Check the scope's power.",
                "error",
                "battery",
            )
        elif battery is not None and (charging is True or battery > low + BATTERY_REARM_MARGIN):
            self._battery_low_sent = False
        if self._was_charging is True and charging is False:
            self._send(
                "Scope stopped charging",
                f"It's running on its battery{f', at {battery}%' if battery is not None else ''}. "
                "Its power supply may be off or unplugged.",
                "warning",
                "battery",
            )
        if charging is not None:
            self._was_charging = charging
        hot = [what for key, what in (("overtemp", "the scope"), ("battery_overtemp", "its battery")) if health.get(key)]
        if hot and not self._hot_sent:
            self._hot_sent = True
            temp = f" ({health['temp_c']:.0f} °C)" if health.get("temp_c") is not None else ""
            self._send("Scope overheating", f"The scope reports {' and '.join(hot)} overheating{temp}.", "error", "temperature")
        elif not hot:
            self._hot_sent = False
        free, floor = health.get("free_gb"), float(limits.get("storage_gb", 5))
        if free is not None and free < floor and not self._storage_sent:
            self._storage_sent = True
            self._send(
                "Scope storage low",
                f"{free:.1f} GB free, below your {floor:g} GB limit. Stacks may stop saving.",
                "warning",
                "storage",
            )
        elif free is not None and free >= floor * 1.2:
            self._storage_sent = False

    def _watch_folder(self):
        """Once a night, while imaging, say so if the MyWorks folder isn't connected."""
        state = self._folder.state()
        if state in ("missing", "empty") and not self._folder_sent:
            self._folder_sent = True
            where = self._image_dir()
            how = "isn't there" if state == "missing" else "is empty"
            self._logger.warning("the MyWorks folder %s: %s", how, where)
            self._send(
                "Stacks folder not connected",
                f"{where} {how}, so the scope's share probably isn't mounted on this Mac. "
                "Stacked cards go without pictures, and framed runs wait to be judged "
                "until it's back.",
                "warning",
                "stacks_folder",
            )
        elif state == "ok" and self._folder_sent:
            self._folder_sent = False
            self._logger.info("the MyWorks folder is connected again")

    def dew(self, margin_c, heater_on):
        """Each tick of a session the automation runs, with the dew margin it used."""
        with self._lock:
            now = self._clock()
            if margin_c is None or not heater_on or margin_c > 0:
                self._dew_since = None
                return
            if self._dew_since is None:
                self._dew_since = now
            if now - self._dew_since >= DEW_WARN_S and not self._night.get("dew_sent"):
                self._night["dew_sent"] = True
                self._save()
                self._send(
                    "Dew point reached",
                    f"The air has been at its dew point ({margin_c:+.1f} °C margin) for "
                    f"{DEW_WARN_S // 60} minutes with the heater on. Watch for dew on the lens.",
                    "warning",
                    "dew",
                )

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
