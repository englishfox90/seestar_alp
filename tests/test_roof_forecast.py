"""The roof forecast: decoding the Clear Sky Chart, rating hours, and the night's verdict."""

from datetime import datetime
from io import BytesIO
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from roof import forecast as f
from roof.planner import night_times

TZ = ZoneInfo("America/Chicago")
SITE = SimpleNamespace(lat=31.0, lon=-99.0, sun_max_deg=-18.0, typical_open_chance=0.5)


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=TZ).timestamp()


def clock(when):
    return datetime.fromtimestamp(when, TZ).strftime("%H:%M")


def chart(cloud_hex, midnight_col=None, cols=82):
    """A chart the size of the real one with every cell of each row one colour."""
    img = Image.new("RGB", (134 + cols * 14, 408), (0, 0, 0))
    px = img.load()
    for col in range(cols):
        for x in range(134 + col * 14, 134 + col * 14 + 12):
            for y, colour in ((83, cloud_hex[col] if isinstance(cloud_hex, list) else cloud_hex), (226, "B70000")):
                px[x, y] = f._hex(colour)
    if midnight_col is not None:
        x = 134 + midnight_col * 14 - 1
        for y in (83, 98, 114, 130):
            px[x, y] = (220, 0, 0)
    out = BytesIO()
    img.save(out, format="GIF")
    return out.getvalue()


def test_chart_cells_decode_to_values():
    rows = f.decode_csc(chart("4E8ECE", midnight_col=10), at("2026-11-13T15:30"), TZ)
    assert len(rows) == 82
    assert rows[0]["cloud"] == 30 and rows[0]["humidity"] == 92


def test_a_midnight_line_ahead_of_the_fetch_is_tonight():
    rows = f.decode_csc(chart("003E7E", midnight_col=9), at("2026-11-13T15:10"), TZ)
    assert rows[0]["at"] == at("2026-11-13T15:00")


def test_a_midnight_line_already_past_is_last_night():
    # Fetched at 01:12 while the chart still starts at the evening run before.
    rows = f.decode_csc(chart("003E7E", midnight_col=3), at("2026-11-14T01:12"), TZ)
    assert rows[0]["at"] == at("2026-11-13T21:00")


def meteo(cloud=0, humidity=70, temp=12.0, dew=6.0, visibility=20000):
    return {"cloud": cloud, "humidity": humidity, "temp_c": temp, "dew_point_c": dew, "visibility_m": visibility}


@pytest.mark.parametrize(
    "row, state",
    [
        (meteo(), f.OPEN),
        # Nov 10: humid with the dew point 1.2 °C away, clear, and the roof stayed open.
        (meteo(humidity=93, temp=15, dew=13.8), f.OPEN),
        (meteo(cloud=40), f.MAYBE),
        (meteo(cloud=100), f.CLOSED),
        (meteo(humidity=96, temp=15, dew=14.2), f.MAYBE),
        (meteo(humidity=98, temp=15, dew=14.6), f.CLOSED),
        (meteo(visibility=800), f.CLOSED),
    ],
)
def test_open_meteo_hours(row, state):
    assert f._state(f.rate_open_meteo(row)) == state


def test_chart_humidity_only_ever_says_maybe():
    assert f._state(f.rate_csc({"cloud": 0, "humidity": 97})) == f.MAYBE
    assert f._state(f.rate_csc({"cloud": 90, "humidity": 50})) == f.CLOSED


def test_rain_and_wind_are_not_rated():
    row = {**meteo(), "rain_pct": 90, "rain_mm": 3.0, "gust_mph": 45}
    assert f._state(f.rate_open_meteo(row)) == f.OPEN


def test_models_that_disagree_give_maybe():
    assert f.combine([f.OPEN, f.CLOSED]) == f.MAYBE
    assert f.combine([f.MAYBE, f.CLOSED]) == f.CLOSED
    assert f.combine([f.OPEN, f.MAYBE]) == f.MAYBE
    assert f.combine([None, f.OPEN]) == f.OPEN
    assert f.combine([]) is None


def hours_of(states, start="2026-11-14T18:00"):
    first = at(start)
    return [
        {"start": first + i * 3600, "end": first + (i + 1) * 3600, "state": s,
         "reasons": [(s, "cloud", 90, "cloud 90%")] if s else []}
        for i, s in enumerate(states)
    ]  # fmt: skip


def test_verdicts():
    assert f.verdict(hours_of([f.OPEN] * 10), clock)["key"] == "open"
    closed = f.verdict(hours_of([f.CLOSED] * 10), clock)
    assert closed["key"] == "closed" and closed["headline"] == "Roof likely closed"
    assert closed["detail"] == "Cloud up to 90%."
    partly = f.verdict(hours_of([f.OPEN] * 4 + [f.CLOSED] * 6), clock)
    assert partly["key"] == "partly" and partly["headline"] == "Roof likely open 18:00–22:00"
    assert f.verdict([], clock)["key"] == "unknown"


def test_the_night_runs_from_half_an_hour_before_sunset_to_half_an_hour_after_sunrise():
    now = at("2026-11-14T12:00")
    times = night_times("2026-11-14", SITE.lat, SITE.lon, SITE.sun_max_deg)
    rows = [{"at": float(h * 3600), **meteo()} for h in range(int(now // 3600), int(now // 3600) + 72)]
    out = f.outlook(SITE, clock, TZ, now, csc_rows=[], meteo_rows=rows)
    tonight_ = out["nights"][0]
    assert tonight_["label"] == "Tonight" and tonight_["key"] == "open"
    assert tonight_["start"] == times["sunset"] - 1800 and tonight_["end"] == times["sunrise"] + 1800
    assert tonight_["hours"][0]["start"] == tonight_["start"]


def test_tonight_only_looks_ahead():
    now = at("2026-11-14T23:30")
    rows = [{"at": float(h * 3600), **meteo()} for h in range(int(now // 3600) - 12, int(now // 3600) + 48)]
    out = f.outlook(SITE, clock, TZ, now, csc_rows=[], meteo_rows=rows)
    assert out["nights"][0]["hours"][0]["start"] == now


def test_dew_note_says_when_the_optics_want_heat():
    rows = [{"at": at("2026-11-14T20:00"), **meteo(temp=12, dew=8)}, {"at": at("2026-11-14T21:00"), **meteo(temp=11, dew=9.5)}]
    assert f.dew_note(rows, at("2026-11-14T18:00"), at("2026-11-15T08:00"), clock) == (
        "Dew point within 2 °C of the temperature from 21:00."
    )


def test_offline_gives_pending_not_an_error():
    out = f.latest(SITE, clock, TZ, now=at("2026-11-14T20:00"))
    assert out["pending"] is True


def test_open_chance_trusts_the_forecast_near_and_the_typical_night_far():
    now = at("2026-11-14T12:00")
    hours = range(int(now // 3600), int(now // 3600) + 16 * 24)
    closed = [{"at": float(h * 3600), **meteo(cloud=100)} for h in hours]
    chance = f.open_chance(SITE, TZ, now, csc_rows=[], meteo_rows=closed)
    assert chance(now + 86400) == pytest.approx(f.OPEN_CHANCE[f.CLOSED])
    assert f.OPEN_CHANCE[f.CLOSED] < chance(now + 6 * 86400) < SITE.typical_open_chance
    assert chance(now + 12 * 86400) == pytest.approx(SITE.typical_open_chance)
    assert chance(now + 20 * 86400) == SITE.typical_open_chance  # past the forecast
    assert chance.forecast_until == (hours[-1] + 1) * 3600


def test_open_chance_without_a_forecast_is_the_typical_night():
    chance = f.open_chance(SITE, TZ, at("2026-11-14T20:00"))
    assert chance(at("2026-11-14T22:00")) == SITE.typical_open_chance
    assert chance.forecast_until is None
