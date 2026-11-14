import logging
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# roof.automation.get_automation() adds a file handler to the "roof" logger only
# when it has none, and that file is roof_automation.log in the working directory:
# the live server's log when tests run from the repo. A handler here keeps test
# events out of it, and records still reach caplog through propagation.
logging.getLogger("roof").addHandler(logging.NullHandler())


@pytest.fixture(autouse=True)
def _keep_roof_automation_off_live_state(tmp_path_factory, monkeypatch):
    """Let no test build the process-wide automation over the live device/ files.

    target_queue(), session_log() and night_history() call the module's own
    get_automation(), so patching it on a page module doesn't cover them. Without
    this, rendering a page builds a real automation on device/roof_state.json, the
    session and target files, and the Discord webhook beside config.toml.
    """
    from device.config import Config
    from roof import automation

    base = tmp_path_factory.mktemp("device")
    monkeypatch.setattr(Config, "path_to_dat", str(base / "config.toml"))
    monkeypatch.setattr(automation, "_instance", None)
    monkeypatch.setattr(automation, "_target_queue", None, raising=False)


@pytest.fixture(autouse=True)
def _keep_roof_forecast_offline(monkeypatch):
    """No test fetches the Clear Sky Chart or Open-Meteo: Tonight and site.json ask
    roof.forecast for the outlook, which would otherwise go to the network."""
    from roof import forecast

    def offline(*args, **kwargs):
        raise OSError("tests stay offline")

    monkeypatch.setattr(forecast, "_csc", forecast._Source(offline))
    monkeypatch.setattr(forecast, "_meteo", forecast._Source(offline))
    monkeypatch.setattr(forecast, "_memo", {"key": None, "value": None})
