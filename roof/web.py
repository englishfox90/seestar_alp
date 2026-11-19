"""The /roof page, its status fragments, and the header badge."""

from urllib.parse import urlparse

import falcon

from roof import alerts as roof_alerts
from roof import config as roof_config
from roof import dashboard, focus, schedule_tools, sentinel, tonight
from roof.automation import get_automation
from roof.sun import sun_altitude_deg


def _front():
    # Imported late: front.app calls install() while starting.
    import front.app as front_app

    return front_app


def _require_same_origin(req):
    """The UI allows any CORS origin, so a web page could otherwise POST here."""
    origin = req.get_header("Origin")
    if not origin:
        return
    allowed = {req.get_header("Host"), req.get_header("X-Forwarded-Host")}
    if urlparse(origin).netloc not in allowed:
        raise falcon.HTTPForbidden(description="cross-origin request refused")


def _form(req) -> dict:
    return req.get_media(default_when_empty={}) or {}


def _schedule_rows():
    rows = []
    for name in schedule_tools.list_schedules():
        if name == schedule_tools.PLAN_FILE:
            continue  # written by the planner; choose "Target list" instead
        items, errors, warnings = schedule_tools.load(schedule_tools.resolve(name))
        rows.append(
            {
                "name": name,
                "errors": errors,
                "warnings": warnings,
                "targets": schedule_tools.target_names(items),
            }
        )
    return rows


def shell_context(context, status=None) -> dict:
    """What a page on the shell needs beyond get_context: the top bar's status and accent."""
    status = get_automation().status() if status is None else status
    return {
        "roof_lines": tonight.roof_status(status),
        "on_accent": tonight._on_accent(context.get("webui_accent_color")),
    }


# The last "Find safety monitors" result, shown until the next search.
_found_monitors: list[dict] | None = None
SENTINEL_WEB_DEFAULT = "http://127.0.0.1:8085"
RUNNING_PHASES = ("opening", "imaging", "parking")


def _sentinel_web(settings) -> str:
    """The PFR Sentinel web server the pier camera and weather come from, or ""."""
    url = settings.pier_image_url
    return url[: -len("/latest")] if url.endswith("/latest") else url


def _session_running(status) -> bool:
    return status.get("phase") in RUNNING_PHASES or status.get("sched_state") == "working"


def live_blockers(settings, status) -> list[str]:
    """Why the automation can't go live yet. Empty when it can."""
    blockers = []
    if settings.lat == 0 and settings.lon == 0:
        blockers.append("Set the site's latitude and longitude, so the automation knows when it's dark.")
    if status.get("safety_detail") == "not read yet":
        blockers.append("Wait a moment for the first safety monitor reading.")
    elif "read failed" in str(status.get("safety_detail", "")) or "not connected" in str(status.get("safety_detail", "")):
        blockers.append("Connect a safety monitor that answers: find one or enter its address below, then Test.")
    return blockers


def safety_panel(settings, status) -> dict:
    """What the Roof safety section shows."""
    return {
        "url": settings.sentinel_url,
        "device": settings.safety_device,
        "safe": status.get("safe"),
        "detail": status.get("safety_detail", ""),
        "found": _found_monitors,
        "sentinel_web": _sentinel_web(settings),
        "sentinel_web_default": SENTINEL_WEB_DEFAULT,
        "site_set": not (settings.lat == 0 and settings.lon == 0),
        "lat": settings.lat,
        "lon": settings.lon,
        "blockers": live_blockers(settings, status),
        "running": _session_running(status),
    }


def _describe(reading) -> str:
    if reading.safe:
        return "connected, and reads safe"
    if reading.connected:
        return f"connected, and reads unsafe ({reading.detail})"
    return reading.detail


class RoofPage:
    @staticmethod
    def on_get(req, resp, telescope_id=1):
        front = _front()
        context = dict(front.get_context(telescope_id, req))
        context["partial_path"] = "roof"
        settings = roof_config.load()
        status = get_automation().status()
        front.render_template(
            req,
            resp,
            "roof.html",
            schedules=_schedule_rows(),
            target_list=schedule_tools.TARGET_LIST,
            target_list_label=schedule_tools.TARGET_LIST_LABEL,
            targets_url=f"/{telescope_id}/targets",
            config_url=f"/{telescope_id}/config",
            settings=settings,
            alerts=roof_alerts.panel(settings),
            safety=safety_panel(settings, status),
            status=status,
            polled=False,
            page_root=f"/{telescope_id}/roof",
            **shell_context(context, status),
            **context,
        )


class RoofStatus:
    @staticmethod
    def on_get(req, resp, telescope_id=1):
        status = get_automation().status()
        _front().render_fragment(
            req,
            resp,
            "partials/roof_status.html",
            status=status,
            polled=True,
            roof_lines=tonight.roof_status(status),
        )


class RoofBadge:
    @staticmethod
    def on_get(req, resp):
        _front().render_fragment(
            req,
            resp,
            "partials/roof_badge.html",
            roof_lines=tonight.roof_status(get_automation().status()),
        )


class RoofSun:
    """Sun altitude for the browser's Auto theme. Reads config only; never touches the scope."""

    @staticmethod
    def on_get(req, resp):
        settings = roof_config.load()
        resp.set_header("Cache-Control", "no-store")
        resp.media = {
            "altitude_deg": round(sun_altitude_deg(settings.lat, settings.lon), 2),
            "known_location": not (settings.lat == 0 and settings.lon == 0),
        }


def _client(req) -> str:
    """Which device sent a request, for the log: its address, and the kind of browser."""
    agent = req.get_header("User-Agent") or ""
    device = next(
        (name for key, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Macintosh", "Mac"), ("Windows", "Windows")) if key in agent),
        "unknown device",
    )
    browser = next(
        (name for key, name in (("Edg/", "Edge"), ("CriOS", "Chrome"), ("Chrome/", "Chrome"), ("FxiOS", "Firefox"), ("Firefox/", "Firefox"), ("Safari/", "Safari")) if key in agent),
        "no browser named" if not agent else "other browser",
    )
    if agent and "Safari/" not in agent and device in ("iPhone", "iPad"):
        browser = "Home Screen app"
    return f"{req.remote_addr or '?'} ({device}, {browser})"


class _Action:
    redirect_to = "roof"

    @classmethod
    def on_post(cls, req, resp, telescope_id=1):
        _require_same_origin(req)
        try:
            get_automation().note_request(f"web: {cls.__name__.removeprefix('Roof')} from {_client(req)}")
        except Exception:
            pass  # a note, never in the way of the action
        message = cls.act(req)
        front = _front()
        if message:
            front.flash(resp, message)
        front.redirect(f"/{telescope_id}/{cls.destination(req)}")

    @classmethod
    def destination(cls, req):
        return cls.redirect_to


class RoofSelect(_Action):
    @staticmethod
    def act(req):
        name = _form(req).get("schedule", "")
        if name and name != schedule_tools.TARGET_LIST and not schedule_tools.resolve(name):
            raise falcon.HTTPBadRequest(description="unknown schedule")
        roof_config.save("schedule", name)
        get_automation().request("schedule_changed")
        return f"Roof automation will use {schedule_tools.label(name) or 'nothing'}."


class RoofEnable(_Action):
    @staticmethod
    def act(req):
        enabled = _form(req).get("enabled") == "true"
        roof_config.save("enabled", enabled)
        get_automation().request("poke")
        return f"Roof automation {'enabled' if enabled else 'disabled'}."


class RoofStart(_Action):
    @staticmethod
    def act(req):
        get_automation().request("start")
        return "Start requested; see the log below."


class RoofStop(_Action):
    """Tonight sends return_to=dashboard and shows the stop in its own state."""

    @staticmethod
    def _from_tonight(req):
        return _form(req).get("return_to") == "dashboard"

    @classmethod
    def destination(cls, req):
        return "dashboard" if cls._from_tonight(req) else cls.redirect_to

    @classmethod
    def act(cls, req):
        get_automation().request("stop")
        tonight.note_stop_requested()
        return None if cls._from_tonight(req) else "Stop + Park requested; see the log below."


class RoofCapture(_Action):
    """Convert the plan currently loaded on the scope into a stored schedule."""

    @staticmethod
    def act(req):
        from device import telescope

        name = schedule_tools.safe_name(_form(req).get("name", ""))
        device = telescope.seestar_dev.get(roof_config.load().device_num)
        if device is None or not device.is_connected:
            return "Capture failed: the scope is not connected."
        plan = schedule_tools.loaded_plan(device)
        if not plan:
            return "Capture failed: no plan is loaded on the scope."
        if f"{name}.json" in schedule_tools.list_schedules():
            return f"Capture failed: {name}.json already exists."
        schedule = schedule_tools.plan_to_schedule(plan)
        schedule_tools.write_schedule(name, schedule)
        return f"Captured {len(plan['list'])} targets as {name}.json."


class AlertsEnable(_Action):
    @staticmethod
    def act(req):
        enabled = _form(req).get("enabled") == "true"
        if enabled and not roof_alerts.read_webhook(roof_alerts.webhook_path()):
            return "Save failed: add a Discord webhook first."
        roof_config.save("discord_enabled", enabled)
        return f"Discord alerts {'on' if enabled else 'off'}."


class AlertsWebhook(_Action):
    @staticmethod
    def act(req):
        url = roof_alerts.parse_webhook(_form(req).get("webhook", ""))
        if not url:
            return "Save failed: that isn't a Discord webhook URL."
        try:
            roof_alerts.write_webhook(roof_alerts.webhook_path(), url)
        except OSError:
            return "Save failed: could not write the webhook file."
        return "Webhook saved. Send a test to check it."


class AlertsWebhookRemove(_Action):
    @staticmethod
    def act(req):
        try:
            roof_alerts.remove_webhook(roof_alerts.webhook_path())
        except OSError:
            return "Remove failed: could not delete the webhook file."
        return "Webhook removed. Nothing will post until you add another."


class AlertsTest(_Action):
    @staticmethod
    def act(req):
        url = roof_alerts.read_webhook(roof_alerts.webhook_path())
        if not url:
            return "Test failed: no webhook saved."
        sent, status = roof_alerts.send_test(url)
        return "Test alert sent. Check the channel." if sent else f"Test failed: {status}"


class AlertsEvents(_Action):
    @staticmethod
    def act(req):
        form = _form(req)
        muted = [kind for kind in roof_alerts.KINDS if f"event_{kind}" not in form]
        roof_config.save("discord_muted", muted)
        return "Alert choices saved."


class SafetyFind(_Action):
    @staticmethod
    def act(req):
        global _found_monitors
        _found_monitors = sentinel.discover()
        n = len(_found_monitors)
        if not n:
            return "Search failed: no Alpaca safety monitor answered. Check it's running, or enter its address."
        return f"Found {n} safety monitor{'s' if n != 1 else ''}. Choose one below."


class SafetyUse(_Action):
    @staticmethod
    def act(req):
        form = _form(req)
        url = sentinel.normalise_url(form.get("url", ""))
        if not url:
            return "Save failed: enter an address such as http://127.0.0.1:11111."
        try:
            device = int(form.get("device", 0))
        except (TypeError, ValueError):
            device = -1
        if device < 0:
            return "Save failed: the device number must be 0 or more."
        roof_config.save("sentinel_url", url)
        roof_config.save("safety_device", device)
        get_automation().request("poke")
        return f"Safety monitor saved: {url}, device {device} is {_describe(sentinel.read(url, device))}."


class SafetyTest(_Action):
    @staticmethod
    def act(req):
        s = roof_config.load()
        reading = sentinel.read(s.sentinel_url, s.safety_device)
        verdict = "Test" if reading.connected else "Test failed:"
        return f"{verdict} {s.sentinel_url}, device {s.safety_device} is {_describe(reading)}."


class SentinelWeb(_Action):
    """PFR Sentinel's web server: the pier camera on Tonight, and its weather for dew."""

    @staticmethod
    def act(req):
        text = _form(req).get("url", "").strip()
        if not text:
            for key in ("pier_image_url", "pier_status_url", "dew_sentinel_url"):
                roof_config.save(key, "")
            return "PFR Sentinel web server turned off. Dew control uses the Open-Meteo forecast."
        base = sentinel.normalise_url(text)
        if not base:
            return "Save failed: enter an address such as http://127.0.0.1:8085."
        roof_config.save("pier_image_url", f"{base}/latest")
        roof_config.save("pier_status_url", f"{base}/status")
        roof_config.save("dew_sentinel_url", f"{base}/status")
        try:
            import requests

            requests.get(f"{base}/status", timeout=3).raise_for_status()
        except Exception as e:
            return f"Saved, but {base} didn't answer ({str(e)[:80]}). Is its web server output on?"
        return f"Saved. {base} answers; the pier camera shows on Tonight."


class RoofMode(_Action):
    """Go live, or back to a dry run, from the page."""

    @staticmethod
    def act(req):
        live = _form(req).get("live") == "true"
        settings = roof_config.load()
        status = get_automation().status()
        if live:
            blockers = live_blockers(settings, status)
            if blockers:
                return "Go live failed: " + " ".join(blockers)
            roof_config.save("dry_run", False)
            get_automation().request("poke")
            return "Live: automation and the buttons on this page now command the scope."
        if not settings.dry_run and _session_running(status):
            return "Dry run refused: a session is running. Stop & park first, so the scope isn't left imaging unwatched."
        roof_config.save("dry_run", True)
        get_automation().request("poke")
        return "Dry run: automation only logs what it would do."


class DefaultDeviceMiddleware:
    """Send page URLs with no device number to the configured scope, and / to Tonight.

    Upstream pages default to device 0, the Federation, when the URL has no
    number. Federation is still there, explicitly, at /0/.
    """

    def __init__(self, router):
        self._router = router

    def process_request(self, req, resp):
        if req.method != "GET" or req.path.split("/")[1].isdigit():
            return
        device = roof_config.load().device_num
        # The bare address opens on Tonight. The Device page stays at /<n>/.
        target = f"/{device}/dashboard" if req.path == "/" else f"/{device}{req.path}"
        if self._router.find(target) is None:
            return
        query = f"?{req.query_string}" if req.query_string else ""
        raise falcon.HTTPFound(target + query)


def install(app):
    """Add the routes and start the automation thread."""
    from roof import night_web, queue_web  # queue_web builds on this module's page helpers

    app.add_middleware(DefaultDeviceMiddleware(app._router))
    pages = {
        "roof": RoofPage,
        "roof/status": RoofStatus,
        "roof/select": RoofSelect,
        "roof/enable": RoofEnable,
        "roof/start": RoofStart,
        "roof/stop": RoofStop,
        "roof/capture": RoofCapture,
        "roof/alerts/enable": AlertsEnable,
        "roof/alerts/webhook": AlertsWebhook,
        "roof/alerts/webhook/remove": AlertsWebhookRemove,
        "roof/alerts/test": AlertsTest,
        "roof/alerts/events": AlertsEvents,
        "roof/safety/find": SafetyFind,
        "roof/safety/use": SafetyUse,
        "roof/safety/test": SafetyTest,
        "roof/safety/sentinel-web": SentinelWeb,
        "roof/mode": RoofMode,
        **dashboard.PAGES,
        **tonight.PAGES,
        **queue_web.PAGES,
        **night_web.PAGES,
    }
    for path, resource in pages.items():
        app.add_route(f"/{path}", resource())
        app.add_route(f"/{{telescope_id:int}}/{path}", resource())
    app.add_route("/roof/badge", RoofBadge())
    app.add_route("/roof/sun", RoofSun())
    focus.listen()
    get_automation().start()
