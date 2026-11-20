"""Name lookups for target search: SIMBAD, then Sesame when SIMBAD is down.

On 2026-11-19 the CDS SIMBAD host answered every name with a "page unavailable" 404
and the Harvard mirror timed out, so search said "Object not found" for M45, M42 and
everything else.
"""

import pytest
import requests

from front import object_lookup
from front.object_lookup import SESAME_URLS, SIMBAD_URLS, lookup

SIMBAD_M42 = """Object M 42  ---  HII  ---  OID=@1234   (@@1234,@@1234)
Coordinates(ICRS,ep=J2000,eq=2000): 05 35 17.3  -05 23 28   (Opt ) A [~ ~ ~] 2003AJ....125..525L
hierarchy:
  ---  ISM  ---
"""
SIMBAD_M45 = """Object M 45  ---  OpC  ---  OID=@675533
Coordinates(ICRS,ep=J2000,eq=2000): 03 46 37.99  +24 10 41.0   (Opt ) C
"""
CDS_DOWN = "<!doctype html><title>Page unavailable — CDS</title>This page is currently unavailable."
# Sesame's real answer for M45 on 2026-11-19, trimmed.
SESAME_M45 = """# m45	#Q031884
#=Si=Simbad, all IDs (via url):    1     0ms (from cache)
%@ 675533
%I.0 Cl Melotte 22
%C.0 OpC
%J 56.65833 +24.17806 = 03:46:37.99 +24:10:41.0
%J.E [~ ~ ~] E 2023A&A...674A..37G
%I M 45
%I NAME Pleiades
#====Done (2026-Nov-20,03:30:26z)====
"""
SESAME_UNKNOWN = """# nosuchthing	#Q031885
#! *** Nothing found ***
#====Done (2026-Nov-20,03:31:00z)====
"""


@pytest.fixture(autouse=True)
def _no_resting_sources():
    object_lookup._resting.clear()
    yield
    object_lookup._resting.clear()


class Reply:
    def __init__(self, text, status_code=200):
        self.text, self.status_code = text, status_code


def _web(answers):
    """A fake requests.get: the first URL prefix that matches decides the reply."""
    asked = []

    def get(url, timeout):
        asked.append(url)
        assert timeout <= 10
        for prefix, answer in answers.items():
            if url.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise requests.ConnectionError("no route")

    return get, asked


def test_simbad_still_comes_first_when_it_answers():
    get, asked = _web({SIMBAD_URLS[0]: Reply(SIMBAD_M42)})
    assert lookup("m42", get) == "05h35m17.3s -05d23m28.0s on"
    assert len(asked) == 1


def test_a_cluster_from_simbad_has_the_lp_filter_off():
    get, _ = _web({SIMBAD_URLS[0]: Reply(SIMBAD_M45)})
    assert lookup("M 45", get) == "03h46m38.0s +24d10m41.0s off"


def test_an_unavailable_page_from_one_simbad_moves_on_to_the_next():
    get, asked = _web({SIMBAD_URLS[0]: Reply(CDS_DOWN, 404), SIMBAD_URLS[1]: Reply(SIMBAD_M42)})
    assert lookup("m42", get) == "05h35m17.3s -05d23m28.0s on"
    assert len(asked) == 2


def test_an_error_page_sent_as_200_moves_on_too():
    get, _ = _web({SIMBAD_URLS[0]: Reply(CDS_DOWN), SIMBAD_URLS[1]: Reply(SIMBAD_M42)})
    assert lookup("m42", get).startswith("05h35m17.3s")


def test_with_simbad_down_sesame_finds_m45():
    # The night of 2026-11-19: CDS's page unavailable, Harvard timing out.
    get, asked = _web({
        SIMBAD_URLS[0]: Reply(CDS_DOWN, 404),
        SIMBAD_URLS[1]: requests.Timeout("timed out"),
        SESAME_URLS[0]: Reply(SESAME_M45),
    })  # fmt: skip
    assert lookup("m45", get) == "03h46m38.0s +24d10m41.0s off"
    assert asked[-1] == SESAME_URLS[0] + "m45"


def test_a_name_with_spaces_reaches_sesame_encoded():
    get, asked = _web({SESAME_URLS[0]: Reply(SESAME_M45)})
    lookup("NGC 7000", get)
    assert asked[-1] == SESAME_URLS[0] + "NGC%207000"


def test_a_sesame_mirror_is_tried_when_the_first_is_down():
    get, asked = _web({SESAME_URLS[2]: Reply(SESAME_M45)})
    assert lookup("m45", get).startswith("03h46m38.0s")
    assert asked[-1].startswith(SESAME_URLS[2])


def test_a_name_no_one_knows_is_not_found():
    get, _ = _web({SIMBAD_URLS[0]: Reply("!! No known catalog could be found"), SESAME_URLS[0]: Reply(SESAME_UNKNOWN)})
    assert lookup("nosuchthing", get) is None


def test_everything_down_is_not_found():
    get, asked = _web({})
    assert lookup("m45", get) is None
    assert len(asked) == len(SIMBAD_URLS) + len(SESAME_URLS)


def test_a_blank_name_asks_nobody():
    get, asked = _web({})
    assert lookup("  ", get) is None
    assert asked == []


@pytest.mark.parametrize(
    "otype, lp",
    [("HII", "on"), ("SNR", "on"), ("PN", "on"), ("GNe", "on"), ("SNR?", "on"), ("EmO", "on"),
     ("OpC", "off"), ("G", "off"), ("GlC", "off"), ("Cl*", "off"), ("*", "off")],
)  # fmt: skip
def test_sesame_object_types_decide_the_lp_filter(otype, lp):
    text = SESAME_M45.replace("%C.0 OpC", f"%C.0 {otype}")
    assert object_lookup._from_sesame(text).endswith(" " + lp)


@pytest.mark.parametrize(
    "ra, dec, expected",
    [
        (56.65833, 24.17806, "03h46m38.0s +24d10m41.0s"),
        (83.82208, -5.39111, "05h35m17.3s -05d23m28.0s"),
        (0.0, -0.5, "00h00m0.0s -00d30m0.0s"),
        (359.99999, 89.99999, "00h00m0.0s +90d00m0.0s"),  # rounding carries over, never 60.0s
        (314.6958, 44.33, "20h58m47.0s +44d19m48.0s"),
    ],
)
def test_degrees_are_written_the_way_simbad_writes_them(ra, dec, expected):
    assert f"{object_lookup._hms(ra)} {object_lookup._dms(dec)}" == expected


def test_the_endpoint_answers_with_the_lookup(monkeypatch):
    import falcon.testing

    from front import app as front_app

    monkeypatch.setattr(object_lookup, "lookup", lambda name: "03h46m38.0s +24d10m41.0s off" if name == "m45" else None)
    api = falcon.App()
    api.add_route("/simbad", front_app.SimbadResource())
    client = falcon.testing.TestClient(api)
    found = client.simulate_get("/simbad", params={"name": "m45"})
    assert found.status_code == 200 and found.text == "03h46m38.0s +24d10m41.0s off"
    missing = client.simulate_get("/simbad", params={"name": "nothing"})
    assert missing.status_code == 404 and missing.text == "Object not found"
    blank = client.simulate_get("/simbad")
    assert blank.status_code == 404


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_a_failed_source_is_passed_over_for_a_while():
    clock = Clock()
    down = {SIMBAD_URLS[0]: Reply(CDS_DOWN, 404), SIMBAD_URLS[1]: requests.Timeout("t"), SESAME_URLS[0]: Reply(SESAME_M45)}
    get, asked = _web(down)
    lookup("m45", get, clock)
    assert len(asked) == 3
    asked.clear()
    clock.t += object_lookup.RESTING_S - 1
    assert lookup("m45", get, clock).startswith("03h46m38.0s")
    assert asked == [SESAME_URLS[0] + "m45"]  # straight to the one that answers
    asked.clear()
    clock.t += 2
    lookup("m45", get, clock)
    assert len(asked) == 3  # rested long enough: SIMBAD is asked again


def test_a_source_that_answers_again_is_used_again_at_once():
    clock = Clock()
    answers = {SIMBAD_URLS[0]: Reply(CDS_DOWN, 404), SIMBAD_URLS[1]: Reply(CDS_DOWN, 404), SESAME_URLS[0]: Reply(SESAME_M45)}
    get, asked = _web(answers)
    lookup("m45", get, clock)
    clock.t += object_lookup.RESTING_S + 1
    answers[SIMBAD_URLS[0]] = Reply(SIMBAD_M45)
    asked.clear()
    lookup("m45", get, clock)
    assert asked == [SIMBAD_URLS[0] + "m45"]
    assert SIMBAD_URLS[0] not in object_lookup._resting


def test_with_every_source_resting_they_are_all_tried_anyway():
    clock = Clock()
    get, asked = _web({})
    lookup("m45", get, clock)
    asked.clear()
    lookup("m45", get, clock)
    assert len(asked) == len(SIMBAD_URLS) + len(SESAME_URLS)


def test_a_name_simbad_does_not_know_does_not_rest_it():
    get, _ = _web({SIMBAD_URLS[0]: Reply("!! No known catalog could be found"), SESAME_URLS[0]: Reply(SESAME_UNKNOWN)})
    lookup("nosuchthing", get)
    assert SIMBAD_URLS[0] not in object_lookup._resting
