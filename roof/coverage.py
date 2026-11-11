"""How much of a saved mosaic stack has any signal.

A framed mosaic is one stack spread across its panels. A run stopped during its first
pass leaves whole panels black, and those holes would darken a later stack of the
nights; one stopped on a later pass only has some panels a few frames short. So a run
counts toward a target's goal when its saved image is covered, however long it ran.

Measured on 2026-11-10: a full 2.3x NGC 7000 run was 7% black (the corners where the
panel columns don't line up, which more frames never fill); a run of one frame was 83%.
"""

import glob
import os

from roof.alerts import PHOTO_CLOCK_SLACK_S

# The scope writes its stack when the run stops; the share may take a while to copy it.
SAVE_WINDOW_S = 30 * 60

# Black share above this means the run left holes. Between the two measured cases, with
# room for framings whose corners waste more.
COVERED_MAX_BLACK = 0.25
# Darker than this, on 0 to 255, is no data. Night sky with signal sits well above it.
NO_DATA_LEVEL = 4
SAMPLE_SIDE = 800


def black_share(path: str) -> float:
    """The share of the image with no data, read from a reduced copy."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    with Image.open(path) as image:
        image.draft("L", (SAMPLE_SIDE, SAMPLE_SIDE))  # a JPEG decodes small, quickly
        image = image.convert("L")
        image.thumbnail((SAMPLE_SIDE, SAMPLE_SIDE))
        histogram = image.histogram()
    total = sum(histogram)
    return sum(histogram[:NO_DATA_LEVEL]) / total if total else 1.0


def run_stack(directory: str, name: str, ended: float) -> str | None:
    """The stack the scope saved when a run ended: the first saved from then on, within
    half an hour, so a later run's stack of the same target isn't taken for it."""
    if not directory or not name or os.path.basename(name) != name:
        return None
    found = []
    for folder in (name, f"{name}_mosaic"):
        for path in glob.glob(os.path.join(glob.escape(directory), glob.escape(folder), "Stacked_*.jpg")):
            if path.endswith("_thn.jpg"):
                continue
            try:
                saved = os.path.getmtime(path)
            except OSError:
                continue
            if ended - PHOTO_CLOCK_SLACK_S <= saved <= ended + SAVE_WINDOW_S:
                found.append((saved, path))
    return min(found)[1] if found else None
