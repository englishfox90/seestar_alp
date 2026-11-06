"""A small append-only record of what the scope said about framed mosaics.

alpyca.log rotates quickly and a start-up rolls it over, which is how the detail of
a framing failure was lost. This file never rotates: a framed item adds about ten
short lines. It is off until the app enables it, so tests can't touch a real file.
Writing here must never affect a session, so failures are ignored.
"""

import os
import threading
from datetime import datetime

FILE_NAME = "roof_framing_diag.log"

_lock = threading.Lock()
_path = None


def enable(directory: str) -> None:
    global _path
    _path = os.path.join(directory, FILE_NAME)


def disable() -> None:
    global _path
    _path = None


def append(line: str) -> None:
    if _path is None:
        return
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _lock, open(_path, "a", encoding="utf-8") as f:
            f.write(f"{stamp} {line}\n")
    except OSError:
        pass
