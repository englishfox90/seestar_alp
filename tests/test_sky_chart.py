"""Tonight's sky chart: sun and moon altitude, and the roof's record and forecast."""

from datetime import datetime
from zoneinfo import ZoneInfo

from roof import sky_chart as sc
from roof.planner import night_times

TZ = ZoneInfo("America/Chicago")
LAT, LON = 31.501, -99.3734


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=TZ).timestamp()


def clock(when):
    return datetime.fromtimestamp(when, TZ).strftime("%H:%M")


WINDOW = night_times("2026-11-14", LAT, LON, -18.0)


def test_horizon_crossings_are_interpolated():
    times = [0.0, 600.0, 1200.0]
    assert sc.crossings(times, [-10.0, 10.0, 20.0]) == [(300.0, "rise")]
    assert sc.crossings(times, [5.0, -5.0, -6.0]) == [(300.0, "set")]
    assert sc.crossings(times, [5.0, 6.0, 7.0]) == []


def test_altitude_maps_to_the_plot_and_clamps():
    assert sc.y_of(90) == 0 and sc.y_of(45, top=45) == 0
    # The horizon is a quarter up from the bottom at any scale, and the floor is -18°.
    assert sc.y_of(0) == 90.0 and sc.y_of(0, top=30) == 90.0 and sc.y_of(-18) == sc.VIEW_H
    # Below the floor the curve carries on past the bottom, for the clip to cut.
    assert sc.y_of(-36) == 150.0


def test_the_plot_top_fits_the_night():
    assert sc.plot_top([-60, 5], [24, -10]) == 45
    assert sc.plot_top([-60], [-20]) == 30
    assert sc.plot_top([10], [88]) == 90


def test_roof_record_before_now_and_forecast_after():
    open_, close = at("2026-11-14T17:00"), at("2026-11-15T08:00")
    now = at("2026-11-14T23:00")
    record = {
        "closed": [{"start": at("2026-11-14T20:00"), "end": at("2026-11-14T21:00")}, {"start": at("2026-11-14T22:30"), "end": None}],
        "unknown": [{"start": at("2026-11-14T16:00"), "end": at("2026-11-14T17:30")}],
    }
    hours = [
        {"start": at("2026-11-14T23:00"), "end": at("2026-11-15T02:00"), "state": "closed"},
        {"start": at("2026-11-15T02:00"), "end": at("2026-11-15T09:00"), "state": "open"},
    ]
    spans = sc.roof_spans(open_, close, now, record, hours)
    assert [(clock(s), clock(e), state) for s, e, state in spans] == [
        ("17:00", "17:30", "unknown"),
        ("17:30", "20:00", "open"),
        ("20:00", "21:00", "closed"),
        ("21:00", "22:30", "open"),
        ("22:30", "23:00", "closed"),
        ("23:00", "02:00", "fc-closed"),
        ("02:00", "08:00", "fc-open"),
    ]


def test_before_the_night_is_all_forecast():
    spans = sc.roof_spans(at("2026-11-14T17:00"), at("2026-11-15T08:00"), at("2026-11-14T12:00"), {"closed": [], "unknown": []},
                          [{"start": at("2026-11-14T17:00"), "end": at("2026-11-15T08:00"), "state": "maybe"}])  # fmt: skip
    assert [state for *_, state in spans] == ["fc-maybe"]


def test_the_chart_shows_the_sun_setting_and_rising():
    now = at("2026-11-14T23:00")
    data = sc.sky_chart(WINDOW, LAT, LON, now, clock, {"closed": [], "unknown": []}, None, True)
    assert data["available"] and data["roof_now"] is True
    # The margin either side puts sunset and sunrise inside the chart, not at its edges.
    assert 5 < data["sunset"] < 15 and 85 < data["sunrise"] < 95
    sun = [b for b in data["bodies"] if b["name"] == "sun"][0]
    assert sun["up"] is False and sun["x"] == data["now"]
    assert data["sun_path"].startswith("M0.0,") and data["sun_path"].count(" L") > 50
    assert f"Sun sets {clock(WINDOW['sunset'])}" in data["label"]


def test_no_site_or_night_means_no_chart():
    assert sc.sky_chart(WINDOW, 0, 0)["available"] is False
    assert sc.sky_chart({}, LAT, LON)["available"] is False


def test_phase_names_and_shadow():
    assert sc.phase_name(1, True) == "New moon" and sc.phase_name(99, False) == "Full moon"
    assert sc.phase_name(24, False) == "Waning crescent" and sc.phase_name(70, True) == "Waxing gibbous"
    assert sc.phase_name(50, True) == "First quarter" and sc.phase_name(50, False) == "Last quarter"
    assert sc.moon_shadow(0, True) == 0 and sc.moon_shadow(100, True) == -2 and sc.moon_shadow(50, False) == 1


def test_rise_and_set_are_marked_on_the_horizon_with_the_moon_phase():
    data = sc.sky_chart(WINDOW, LAT, LON, at("2026-11-14T20:00"), clock)
    sun = [e for e in data["horizon_events"] if e["body"] == "sun"]
    assert [e["text"] for e in sun] == [f"Sunset {clock(WINDOW['sunset'])}", f"Sunrise {clock(WINDOW['sunrise'])}"]
    assert sun[0]["at"] == data["sunset"]
    moon = [e for e in data["horizon_events"] if e["body"] == "moon"]
    assert all(e["text"].startswith(("Moonrise", "Moonset")) for e in moon)
    if data["phase"]:
        assert data["phase"]["name"] in data["label"] and data["phase"]["y"] < data["horizon_pct"]
    assert 40 < data["middle"] < 60 and len(data["grid"]) > 10


def test_our_building_sits_between_its_neighbours_with_real_roofs():
    from roof import site_status

    def row(n, is_open, updated="2026-11-14T07:33:00Z"):
        return {"device_number": n, "name": f"Building {n}", "is_open": is_open, "state_update": updated}

    now = datetime.fromisoformat("2026-11-14T07:40:00+00:00").timestamp()
    rows = [row(17, True), row(18, False), row(19, True), row(20, False, "2026-11-13T00:00:00Z")]
    street = site_status.street(rows, 19, clock, now)
    assert [(b["number"], b["open"], b["ours"]) for b in street] == [(18, False, False), (19, True, True), (20, None, False)]
    # Drawn, all three show our roof's state.
    data = sc.sky_chart(WINDOW, LAT, LON, now, clock, roof_safe=False, buildings=street)
    assert [b["open"] for b in data["buildings"]] == [True, True, True] and data["roof_now"] is True
    # The feed missing ours: it reads the roof monitor instead.
    data = sc.sky_chart(WINDOW, LAT, LON, now, clock, roof_safe=False, buildings=site_status.street(rows[:2], 19, clock, now))
    assert [b["label"] for b in data["buildings"]] == ["17", "18", "19"]
    assert data["roof_now"] is False and data["buildings"][2]["ours"]
    # No feed at all: ours alone.
    assert sc.sky_chart(WINDOW, LAT, LON, now, clock, roof_safe=True)["buildings"] == [{"label": "", "open": True, "ours": True}]
