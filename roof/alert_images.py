"""Pictures for Discord: the night ahead as a timeline, and the night's stacks together.

Drawn with Pillow, small enough to post quickly. For a person to look at: nothing
here decides anything, and a failure only means a message goes without its picture.
"""

import io
import os
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont, ImageOps

# Night colours, close to the app's night theme.
CANVAS = (12, 13, 16)
PANEL = (28, 28, 31)
TEXT = (232, 232, 234)
MUTED = (133, 133, 140)
GRID = (52, 52, 58)
DARK = (34, 48, 74)
MOON = (214, 200, 150)
ROOF = {"open": (62, 196, 109), "maybe": (240, 160, 48), "closed": (255, 98, 89)}
BLOCKS = ((95, 164, 248), (176, 132, 247), (86, 204, 214), (240, 132, 180))

WIDTH = 1000
LEFT, RIGHT = 92, 24  # room for lane names, and a margin
LANE_H, LANE_GAP = 30, 12
MARGIN_S = 30 * 60  # the timeline runs from sunset - MARGIN_S to sunrise + MARGIN_S

COLLAGE_TILE = 640
COLLAGE_MAX = 6


def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow without FreeType: one fixed size
        return ImageFont.load_default()


def _png(image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _fit(draw, text, font, width) -> str:
    """Text shortened with an ellipsis to fit ``width`` pixels."""
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text + "…" if text else ""


def timeline(
    window, hours=(), blocks=(), moon_up=(), moon_lit=None, clock=None
) -> bytes | None:
    """The night ahead as a PNG: dark hours, moon, roof forecast and planned targets.

    ``window`` has sunset, sunrise, dark_start and dark_end; ``hours`` is the
    forecast's hours (start, end, state); ``blocks`` are the plan's (name, start,
    end); ``moon_up`` is spans (start, end) when the moon is above the horizon.
    """
    window = window or {}
    sunset, sunrise = window.get("sunset"), window.get("sunrise")
    if sunset is None or sunrise is None or sunrise <= sunset:
        return None
    clock = clock or (lambda t: datetime.fromtimestamp(t).strftime("%H:%M"))
    start, end = sunset - MARGIN_S, sunrise + MARGIN_S
    plot_w = WIDTH - LEFT - RIGHT

    def x(when):
        return LEFT + max(0.0, min(1.0, (when - start) / (end - start))) * plot_w

    lanes = ["Dark", "Moon", "Roof", "Plan"]
    top = 20
    height = top + len(lanes) * (LANE_H + LANE_GAP) + 40
    image = Image.new("RGB", (WIDTH, height), CANVAS)
    draw = ImageDraw.Draw(image)
    small, label = _font(14), _font(15)

    def lane_top(i):
        return top + i * (LANE_H + LANE_GAP)

    for i, name in enumerate(lanes):
        y = lane_top(i)
        draw.rounded_rectangle(
            (LEFT, y, LEFT + plot_w, y + LANE_H), radius=6, fill=PANEL
        )
        draw.text(
            (LEFT - 12, y + LANE_H / 2), name, font=label, fill=MUTED, anchor="rm"
        )

    # Hour lines and labels, on the hour.
    first_hour = int(start // 3600 + 1) * 3600
    bottom = lane_top(len(lanes) - 1) + LANE_H
    for when in range(first_hour, int(end), 3600):
        px = x(when)
        draw.line((px, top, px, bottom), fill=GRID, width=1)
        if (when // 3600) % 2 == 0:
            draw.text(
                (px, bottom + 8), clock(when), font=small, fill=MUTED, anchor="mt"
            )

    def bar(i, a, b, colour, text=None, text_fill=CANVAS):
        if b is None or a is None or b <= a:
            return
        x0, x1 = x(a), x(b)
        if x1 - x0 < 1:
            return
        y = lane_top(i)
        draw.rounded_rectangle((x0, y + 3, x1, y + LANE_H - 3), radius=5, fill=colour)
        if text and x1 - x0 > 30:
            draw.text(
                (x0 + 7, y + LANE_H / 2),
                _fit(draw, text, small, x1 - x0 - 12),
                font=small,
                fill=text_fill,
                anchor="lm",
            )

    dark_start, dark_end = window.get("dark_start"), window.get("dark_end")
    if dark_start is not None and dark_end is not None:
        hours_dark = (dark_end - dark_start) / 3600
        bar(
            0,
            dark_start,
            dark_end,
            DARK,
            f"{clock(dark_start)} to {clock(dark_end)}, {hours_dark:.1f} h",
            TEXT,
        )
    lit = f"{round(moon_lit)}% lit" if moon_lit is not None else None
    for a, b in moon_up:
        bar(1, a, b, MOON, lit)
    merged = []
    for h in sorted(hours, key=lambda h: h["start"]):
        if (
            merged
            and merged[-1]["state"] == h["state"]
            and h["start"] <= merged[-1]["end"] + 1
        ):
            merged[-1]["end"] = max(merged[-1]["end"], h["end"])
        else:
            merged.append({"start": h["start"], "end": h["end"], "state": h["state"]})
    for h in merged:
        bar(
            2, h["start"], h["end"], ROOF.get(h["state"], GRID), h["state"].capitalize()
        )
    for k, block in enumerate(blocks):
        bar(3, block["start"], block["end"], BLOCKS[k % len(BLOCKS)], block["name"])
    return _png(image)


def collage(stacks) -> bytes | None:
    """The night's stacks side by side, each named. ``stacks`` is (name, path) pairs;
    unreadable files are left out. None when there's nothing to show."""
    tiles = []
    font = _font(22)
    for name, path in list(stacks)[:COLLAGE_MAX]:
        try:
            with Image.open(path) as source:
                tile = ImageOps.fit(
                    ImageOps.exif_transpose(source).convert("RGB"),
                    (COLLAGE_TILE, COLLAGE_TILE),
                )
        except (OSError, ValueError):
            continue
        draw = ImageDraw.Draw(tile, "RGBA")
        draw.rectangle(
            (0, COLLAGE_TILE - 40, COLLAGE_TILE, COLLAGE_TILE), fill=(0, 0, 0, 160)
        )
        draw.text(
            (14, COLLAGE_TILE - 20),
            _fit(draw, name, font, COLLAGE_TILE - 28),
            font=font,
            fill=TEXT,
            anchor="lm",
        )
        tiles.append(tile)
    if not tiles:
        return None
    columns = 1 if len(tiles) == 1 else 2 if len(tiles) <= 4 else 3
    rows = -(-len(tiles) // columns)
    gap = 6
    sheet = Image.new(
        "RGB",
        (
            columns * COLLAGE_TILE + (columns - 1) * gap,
            rows * COLLAGE_TILE + (rows - 1) * gap,
        ),
        CANVAS,
    )
    for k, tile in enumerate(tiles):
        sheet.paste(
            tile,
            (
                (k % columns) * (COLLAGE_TILE + gap),
                (k // columns) * (COLLAGE_TILE + gap),
            ),
        )
    out = io.BytesIO()
    sheet.save(out, format="JPEG", quality=85)
    return out.getvalue()


def readable(path) -> bool:
    return bool(path) and os.path.isfile(path)
