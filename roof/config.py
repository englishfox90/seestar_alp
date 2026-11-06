"""Settings for roof automation: the [roof] section of config.toml.

Read fresh every tick, so a hand edit takes effect without a restart. The web
page only ever writes ``enabled`` and ``schedule``; ``dry_run`` is file-only, and
only a literal ``dry_run = false`` arms the automation.
"""

from dataclasses import dataclass

import tomlkit

from device.config import Config

SECTION = "roof"
WRITABLE_KEYS = {"enabled", "schedule"}


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    dry_run: bool = True
    schedule: str = ""
    device_num: int = 1
    sentinel_url: str = "http://127.0.0.1:11111"
    safety_device: int = 0
    sun_max_deg: float = -18.0
    min_target_alt_deg: float = 0.0  # 0 turns the gate off
    poll_s: float = 10.0
    start_settle_s: float = 120.0
    stop_grace_s: float = 30.0
    dew_enabled: bool = False
    dew_on_margin_c: float = 4.0
    dew_off_margin_c: float = 6.0
    dew_power: int = 30
    dew_sentinel_url: str = "http://127.0.0.1:8085/status"
    pier_image_url: str = "http://127.0.0.1:8085/latest"
    pier_status_url: str = "http://127.0.0.1:8085/status"
    discord_enabled: bool = False
    lat: float = 0.0
    lon: float = 0.0


def _unwrap(value):
    return value.unwrap() if hasattr(value, "unwrap") else value


def _number(key, default, cast=float):
    try:
        return cast(_unwrap(Config.get_toml(SECTION, key, default)))
    except (TypeError, ValueError):
        return default


def load() -> Settings:
    get = lambda key, default: _unwrap(Config.get_toml(SECTION, key, default))  # noqa: E731
    return Settings(
        enabled=get("enabled", False) is True,
        dry_run=get("dry_run", True) is not False,
        schedule=str(get("schedule", "")),
        device_num=_number("device_num", 1, int),
        sentinel_url=str(get("sentinel_url", Settings.sentinel_url)).rstrip("/"),
        safety_device=_number("safety_device", 0, int),
        sun_max_deg=_number("sun_max_deg", -18.0),
        min_target_alt_deg=_number("min_target_alt_deg", 0.0),
        poll_s=max(1.0, _number("poll_s", 10.0)),
        start_settle_s=max(0.0, _number("start_settle_s", 120.0)),
        stop_grace_s=max(0.0, _number("stop_grace_s", 30.0)),
        dew_enabled=get("dew_enabled", False) is True,
        dew_on_margin_c=_number("dew_on_margin_c", 4.0),
        dew_off_margin_c=_number("dew_off_margin_c", 6.0),
        dew_power=min(100, max(0, _number("dew_power", 30, int))),
        dew_sentinel_url=str(get("dew_sentinel_url", Settings.dew_sentinel_url)),
        pier_image_url=str(get("pier_image_url", Settings.pier_image_url)),
        pier_status_url=str(get("pier_status_url", Settings.pier_status_url)),
        discord_enabled=get("discord_enabled", False) is True,
        lat=float(_unwrap(Config.get_toml("seestar_initialization", "lat", 0)) or 0),
        lon=float(_unwrap(Config.get_toml("seestar_initialization", "long", 0)) or 0),
    )


def save(key: str, value) -> None:
    """Persist one page-writable setting to config.toml."""
    if key not in WRITABLE_KEYS:
        raise ValueError(f"{key} cannot be changed from the web page")
    if SECTION not in Config._dict:
        Config._dict[SECTION] = tomlkit.table()
    Config._dict[SECTION][key] = value
    Config.save_toml()
