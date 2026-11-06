"""The /roof page, its status fragments, and the header badge."""

from urllib.parse import urlparse

import falcon

from roof import alerts as roof_alerts
from roof import config as roof_config
from roof import dashboard, focus, schedule_tools, tonight
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
            settings=settings,
            alerts=roof_alerts.panel(settings),
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


class _Action:
    redirect_to = "roof"

    @classmethod
    def on_post(cls, req, resp, telescope_id=1):
        _require_same_origin(req)
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


class DefaultDeviceMiddleware:
    """Send page URLs with no device number to the configured scope.

    Upstream pages default to device 0, the Federation, when the URL has no
    number. Federation is still there, explicitly, at /0/.
    """

    def __init__(self, router):
        self._router = router

    def process_request(self, req, resp):
        if req.method != "GET" or req.path.split("/")[1].isdigit():
            return
        device = roof_config.load().device_num
        target = f"/{device}{req.path}"
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
