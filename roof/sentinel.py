"""ASCOM Alpaca SafetyMonitor client: PFR Sentinel's, or any other.

Safe ONLY if Connected is true AND IsSafe is true. Anything else (false, missing,
HTTP error, malformed reply) reads UNSAFE.
"""

import json
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

TIMEOUT_S = 5
CLIENT_ID = 78


@dataclass(frozen=True)
class SafetyReading:
    safe: bool
    connected: bool | None
    is_safe: bool | None
    detail: str


def _get(base: str, member: str):
    r = requests.get(
        f"{base}/{member}",
        params={"ClientID": CLIENT_ID, "ClientTransactionID": 1},
        timeout=TIMEOUT_S,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("ErrorNumber"):
        raise ValueError(f"{member}: {body.get('ErrorMessage') or body['ErrorNumber']}")
    return body.get("Value")


def read(url: str, device: int) -> SafetyReading:
    base = f"{url}/api/v1/safetymonitor/{device}"
    try:
        connected = _get(base, "connected")
        if connected is not True:
            # Connected is a server-wide flag that resets when Sentinel restarts.
            # Ask for it back, but this reading still counts as unsafe.
            requests.put(
                f"{base}/connected",
                data={
                    "Connected": "True",
                    "ClientID": CLIENT_ID,
                    "ClientTransactionID": 2,
                },
                timeout=TIMEOUT_S,
            ).raise_for_status()
            return SafetyReading(False, False, None, "not connected; reconnecting")
        is_safe = _get(base, "issafe")
        if is_safe is True:
            return SafetyReading(True, True, True, "safe")
        return SafetyReading(False, True, False, "IsSafe is not true")
    except Exception as e:
        return SafetyReading(False, None, None, f"read failed: {str(e)[:120]}")


DISCOVERY_PORT = 32227
DISCOVERY_MESSAGE = b"alpacadiscovery1"
# Asked directly as well as by discovery: PFR Sentinel listens on this computer
# only by default, where a broadcast doesn't reach it.
LOCAL_SERVERS = ("http://127.0.0.1:11111",)


def normalise_url(text: str) -> str | None:
    """An Alpaca server address as http://host:port, or None if it isn't one."""
    text = (text or "").strip().rstrip("/")
    if not text or any(c.isspace() for c in text):
        return None
    if "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return None
    try:
        parsed.port
    except ValueError:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _discover_servers(timeout_s: float) -> set[str]:
    """Alpaca servers answering discovery on this network, as base URLs."""
    found = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.2)
        for address in ("255.255.255.255", "127.0.0.1"):
            try:
                sock.sendto(DISCOVERY_MESSAGE, (address, DISCOVERY_PORT))
            except OSError:
                pass
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                data, (host, _) = sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                port = int(json.loads(data).get("AlpacaPort"))
            except (ValueError, TypeError, AttributeError):
                continue
            found.add(f"http://{host}:{port}")
    finally:
        sock.close()
    return found


def safety_monitors(server: str) -> list[dict]:
    """The SafetyMonitor devices one Alpaca server offers. Empty if it doesn't answer."""
    try:
        r = requests.get(f"{server}/management/v1/configureddevices", timeout=2)
        r.raise_for_status()
        devices = r.json().get("Value") or []
    except Exception:
        return []
    return [
        {
            "url": server,
            "device": int(d.get("DeviceNumber", 0)),
            "name": str(d.get("DeviceName") or "Safety monitor"),
            "local": urlparse(server).hostname in ("127.0.0.1", "localhost"),
        }
        for d in devices
        if isinstance(d, dict) and str(d.get("DeviceType", "")).lower() == "safetymonitor"
    ]


def discover(timeout_s: float = 2.0, servers=None) -> list[dict]:
    """Every SafetyMonitor found on this computer and network, by name."""
    candidates = set(LOCAL_SERVERS) | (_discover_servers(timeout_s) if servers is None else set(servers))
    monitors = [m for server in sorted(candidates) for m in safety_monitors(server)]
    # 127.0.0.1 and this computer's own address can be the same server.
    unique = {}
    for m in monitors:
        unique.setdefault((m["name"], m["device"], urlparse(m["url"]).port), m)
    return sorted(unique.values(), key=lambda m: (not m["local"], m["name"], m["url"]))
