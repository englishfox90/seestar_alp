"""PFR Sentinel ASCOM Alpaca SafetyMonitor client.

Safe ONLY if Connected is true AND IsSafe is true. Anything else (false, missing,
HTTP error, malformed reply) reads UNSAFE.
"""

from dataclasses import dataclass

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
