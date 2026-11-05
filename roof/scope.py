"""The automation's view of the scope: direct, in-process calls into the device.

RealScope commands hardware. DryRunScope reads the real scope but only pretends
to act, keeping a virtual state so the loop runs through its transitions.
"""

import threading
from dataclasses import dataclass, replace

from roof.schedule_tools import SETTING_ACTIONS
from roof.schedule_tools import load as schedule_load


@dataclass(frozen=True)
class ScopeState:
    connected: bool = False
    name: str = ""
    parked: bool | None = None  # firmware mount.close; None = unknown
    tracking: bool | None = None
    sched_state: str = "unknown"
    item_number: int = 0
    item_count: int = 0
    target: str = ""
    view_state: str = ""
    view_stage: str = ""
    plan_state: str = ""
    mosaic_scale: float | None = None
    busy: bool = False  # a start-up or scheduler thread is still alive


def _failed(res) -> str | None:
    """Failure text for a device reply, or None when it looks fine."""
    if not isinstance(res, dict):
        return f"unexpected reply: {res!r}"[:160]
    if res.get("error"):
        return str(res["error"])
    code = res.get("code", 0)
    if code:
        return str(res.get("result") or f"code {code}")
    result = res.get("result")
    if isinstance(result, str) and result.startswith("Error"):
        return result  # send_message_param_sync's timeout shape
    return None


class RealScope:
    dry_run = False

    def __init__(self, get_device):
        self._get_device = get_device

    def read(self) -> ScopeState:
        dev = self._get_device()
        if dev is None or not dev.is_connected:
            return ScopeState(connected=False, name=getattr(dev, "device_name", ""))
        sched = dev.schedule
        items = sched.get("list") or []
        number = int(sched.get("item_number") or 0)
        target = ""
        if 1 <= number <= len(items):
            target = items[number - 1].get("params", {}).get("target_name", "")
        state = ScopeState(
            connected=True,
            name=dev.device_name,
            sched_state=str(sched.get("state", "unknown")),
            item_number=number if number <= len(items) else 0,
            item_count=len(items),
            target=target,
            busy=self.busy(),
        )
        device_state = self._rpc(dev, "get_device_state")
        mount = device_state.get("mount")
        if isinstance(mount, dict):
            parked, tracking = mount.get("close"), mount.get("tracking")
            state = replace(
                state,
                parked=parked if isinstance(parked, bool) else None,
                tracking=tracking if isinstance(tracking, bool) else None,
            )
        mosaic = (device_state.get("setting") or {}).get("mosaic")
        if isinstance(mosaic, dict) and isinstance(mosaic.get("scale"), (int, float)):
            state = replace(state, mosaic_scale=float(mosaic["scale"]))
        app_state = self._rpc(dev, "iscope_get_app_state")
        view = app_state.get("View") or {}
        plan = app_state.get("ViewPlan") or {}
        return replace(
            state,
            view_state=str(view.get("state", "")),
            view_stage=str(view.get("stage", "")),
            plan_state=str(plan.get("state", "")),
            target=target or str(view.get("target_name", "")),
        )

    @staticmethod
    def _rpc(dev, method) -> dict:
        try:
            res = dev.send_message_param_sync({"method": method})
        except Exception:
            return {}
        result = res.get("result") if isinstance(res, dict) else None
        return result if isinstance(result, dict) else {}

    def busy(self) -> bool:
        dev = self._get_device()
        if dev is None:
            return False
        sched_thread = getattr(dev, "scheduler_thread", None)
        if sched_thread is not None and sched_thread.is_alive():
            return True
        prefix = f"start-up-thread:{dev.device_name}"
        return any(t.name == prefix and t.is_alive() for t in threading.enumerate())

    def _device(self):
        dev = self._get_device()
        if dev is None:
            raise RuntimeError("device is not available")
        return dev

    def open_arm(self, lat: float, lon: float) -> str | None:
        """Open the arm with NO alignment. Returns failure text or None.

        3ppa is always false: this rig is permanently polar-aligned.
        """
        return _failed(
            self._device().action_start_up_sequence(
                {
                    "lat": lat,
                    "lon": lon,
                    "3ppa": False,
                    "auto_focus": False,
                    "dark_frames": False,
                }
            )
        )

    def import_schedule(self, path: str) -> str | None:
        return _failed(
            self._device().import_schedule({"filepath": path, "is_retain_state": False})
        )

    def apply_setting(self, item: dict) -> str | None:
        dev = self._device()
        action, params = item["action"], item.get("params", {})
        if action not in SETTING_ACTIONS:
            return None
        if action == "action_set_exposure":
            dev.action_set_exposure(params)
            return None
        if action == "action_set_dew_heater":
            return _failed(dev.action_set_dew_heater(params))
        return _failed(dev.send_message_param_sync({"method": action, "params": params}))

    def start_scheduler(self, start_item: int) -> str | None:
        return _failed(self._device().start_scheduler({"start_item": start_item}))

    def ensure_master(self) -> None:
        dev = self._device()
        if not dev.is_client_master():
            dev.send_message_param_sync(
                {"method": "set_setting", "params": {"master_cli": True}}
            )

    def clear_view_plan(self) -> str | None:
        return _failed(self._device().send_message_param_sync({"method": "clear_view_plan"}))

    def stop_scheduler(self) -> None:
        # Replies "not running" when idle, which is fine here.
        self._device().stop_scheduler({})

    def stop_view(self) -> str | None:
        return _failed(self._device().send_message_param_sync({"method": "iscope_stop_view"}))

    def skip_item(self) -> str | None:
        return _failed(self._device().skip_scheduler_cur_item({}))

    def park(self) -> str | None:
        return _failed(self._device().send_message_param_sync({"method": "scope_park"}))


class DryRunScope:
    """Reads the real scope; every action is simulated."""

    dry_run = True

    def __init__(self, real: RealScope):
        self._real = real
        self._parked: bool | None = None
        self._sched = "stopped"
        self._item = 0
        self._count = 0

    def read(self) -> ScopeState:
        real = self._real.read()
        if self._parked is None:
            self._parked = real.parked
        if real.sched_state == "working":
            return real  # a real session is running; show it as it is
        parked = self._parked if self._parked is not None else real.parked
        return replace(
            real,
            parked=parked,
            sched_state=self._sched,
            item_number=self._item,
            item_count=self._count or real.item_count,
            busy=False,
        )

    def busy(self):
        return False

    def open_arm(self, lat, lon):
        self._parked = False
        self._sched = "complete"

    def import_schedule(self, path):
        self._sched = "stopped"
        self._count = len(schedule_load(path)[0])

    def skip_item(self):
        self._item += 1
        if self._item > self._count:
            self._sched = "complete"
            self._item = 0

    def apply_setting(self, item):
        return None

    def start_scheduler(self, start_item):
        self._sched = "working"
        self._item = start_item

    def ensure_master(self):
        return None

    def clear_view_plan(self):
        return None

    def stop_scheduler(self):
        self._sched = "stopped"
        self._item = 0

    def stop_view(self):
        return None

    def park(self):
        self._parked = True
