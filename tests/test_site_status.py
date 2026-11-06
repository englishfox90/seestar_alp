"""Starfront's public status on Tonight, and which night the sky map means."""

from types import SimpleNamespace

from roof import site_status, sky_window

SITE = SimpleNamespace(lat=31.501, lon=-99.3734, sun_max_deg=-18.0, min_target_alt_deg=20.0, site_building=19)
NOW = 1793938000.0  # the evening of 2026-11-05 at the site


def clock(when):
    return f"t{int(when)}"


def _row(number=19, is_open=False, updated="2026-11-06T04:00:00Z"):
    return {"device_number": number, "name": f"Building {number}", "is_open": is_open, "state_update": updated}


def test_picks_out_this_building():
    found = site_status.building_roof([_row(1, True), _row(19)], 19, clock, 1793938000.0)
    assert found["name"] == "Building 19" and found["open"] is False


def test_an_old_reading_is_called_stale():
    found = site_status.building_roof([_row(updated="2026-11-01T00:00:00Z")], 19, clock, NOW)
    assert found["stale"] is True


def test_a_missing_building_or_bad_feed_gives_nothing():
    assert site_status.building_roof([_row(1)], 19, clock, NOW) is None
    assert site_status.building_roof(None, 19, clock, NOW) is None
    assert site_status.building_roof(["nonsense"], 19, clock, NOW) is None


def test_news_is_newest_first_limited_and_toned():
    rows = [
        {"title": f"T{i}", "body": "b", "level": "alert", "date": f"2026-11-0{i}T00:00:00Z"} for i in range(1, 6)
    ] + [{"nothing": "here"}]
    items = site_status.news_items(rows, clock)
    assert [i["title"] for i in items] == ["T5", "T4", "T3"] and items[0]["tone"] == "danger"


def _site(monkeypatch, rows, safe, news=()):
    monkeypatch.setattr(site_status._roof_feed, "latest", lambda: rows)
    monkeypatch.setattr(site_status._news_feed, "latest", lambda: list(news))
    return site_status.site_data(SITE, safe, clock, now=NOW)


def test_flags_only_a_safe_reading_with_a_closed_building(monkeypatch):
    assert _site(monkeypatch, [_row(is_open=False)], True)["disagree"] is True
    assert _site(monkeypatch, [_row(is_open=True)], False)["disagree"] is False  # clouds, say
    assert _site(monkeypatch, [_row(is_open=False)], False)["disagree"] is False


def test_a_stale_reading_never_raises_the_flag(monkeypatch):
    rows = [_row(updated="2026-11-01T00:00:00Z")]
    assert _site(monkeypatch, rows, True)["disagree"] is False


def test_the_scheduled_roof_times_use_starfronts_offsets(monkeypatch):
    times = sky_window.night_times("2026-11-05", SITE.lat, SITE.lon, SITE.sun_max_deg)
    schedule = _site(monkeypatch, [], True)["schedule"]
    assert schedule == {
        "open": clock(times["sunset"] - 900),
        "close": clock(times["sunrise"] + 300),
    }


def test_a_failed_fetch_keeps_the_last_answer():
    replies = [SimpleNamespace(raise_for_status=lambda: None, json=lambda: [_row()])]

    def get(url, timeout):
        if replies:
            return replies.pop()
        raise OSError("down")

    now = [0.0]
    feed = site_status._Feed("u", clock=lambda: now[0], get=get)
    assert feed.latest() == [_row()] and feed.ok
    now[0] += site_status.FETCH_INTERVAL_S
    assert feed.latest() == [_row()] and not feed.ok


def test_it_fetches_at_most_once_a_minute():
    calls = []

    def get(url, timeout):
        calls.append(url)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: [])

    feed = site_status._Feed("u", clock=lambda: 5.0, get=get)
    feed.latest()
    feed.latest()
    assert len(calls) == 1


def test_a_reply_that_is_not_a_list_is_ignored():
    feed = site_status._Feed(
        "u", clock=lambda: 0.0, get=lambda u, timeout: SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"a": 1})
    )
    assert feed.latest() is None and not feed.ok


def test_building_off_means_no_camera_and_no_fetch(monkeypatch):
    off = SimpleNamespace(**{**vars(SITE), "site_building": 0})
    assert "camera" not in site_status.links(off)
    monkeypatch.setattr(site_status._news_feed, "latest", lambda: [])
    assert site_status.site_data(off, True, clock, now=NOW)["building"] is None


def test_tonight_on_the_map_is_the_night_in_progress():
    # 21:00 on the 5th, and 03:00 the next morning, are both the night of the 5th.
    assert sky_window.current_night(NOW, SITE) == "2026-11-05"
    assert sky_window.current_night(NOW + 6 * 3600, SITE) == "2026-11-05"


def test_after_the_dark_it_means_the_coming_night():
    end = sky_window.night_times("2026-11-05", SITE.lat, SITE.lon, SITE.sun_max_deg)["dark_end"]
    assert sky_window.current_night(end + 60, SITE) == "2026-11-06"  # a morning, before noon
    assert sky_window.current_night(end + 8 * 3600, SITE) == "2026-11-06"  # the afternoon




def test_tonight_on_the_map_rolls_forward_with_days():
    assert sky_window.sky_window(1, clock, now=NOW, settings=SITE)["night"] == "2026-11-06"
