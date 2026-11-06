"""The /roof page, its status fragments, and the header badge."""

from urllib.parse import urlparse

import falcon

from roof import config as roof_config
from roof import dashboard, focus, schedule_tools
from roof.automation import get_automation


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


class RoofPage:
    @staticmethod
    def on_get(req, resp, telescope_id=1):
        front = _front()
        context = dict(front.get_context(telescope_id, req))
        context["partial_path"] = "roof"
        settings = roof_config.load()
        front.render_template(
            req,
            resp,
            "roof.html",
            schedules=_schedule_rows(),
            target_list=schedule_tools.TARGET_LIST,
            target_list_label=schedule_tools.TARGET_LIST_LABEL,
            targets_url=f"/{telescope_id}/targets",
            settings=settings,
            status=get_automation().status(),
            page_root=f"/{telescope_id}/roof",
            **context,
        )


class RoofStatus:
    @staticmethod
    def on_get(req, resp, telescope_id=1):
        _front().render_fragment(
            req, resp, "partials/roof_status.html", status=get_automation().status()
        )


class RoofBadge:
    @staticmethod
    def on_get(req, resp):
        _front().render_fragment(
            req, resp, "partials/roof_badge.html", status=get_automation().status()
        )


class _Action:
    redirect_to = "roof"

    @classmethod
    def on_post(cls, req, resp, telescope_id=1):
        _require_same_origin(req)
        message = cls.act(req)
        front = _front()
        if message:
            front.flash(resp, message)
        front.redirect(f"/{telescope_id}/{cls.redirect_to}")


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
    @staticmethod
    def act(req):
        get_automation().request("stop")
        return "Stop + Park requested; see the log below."


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
    from roof import queue_web  # it builds on this module's page helpers

    app.add_middleware(DefaultDeviceMiddleware(app._router))
    pages = {
        "roof": RoofPage,
        "roof/status": RoofStatus,
        "roof/select": RoofSelect,
        "roof/enable": RoofEnable,
        "roof/start": RoofStart,
        "roof/stop": RoofStop,
        "roof/capture": RoofCapture,
        **dashboard.PAGES,
        **queue_web.PAGES,
    }
    for path, resource in pages.items():
        app.add_route(f"/{path}", resource())
        app.add_route(f"/{{telescope_id:int}}/{path}", resource())
    app.add_route("/roof/badge", RoofBadge())
    focus.listen()
    get_automation().start()
