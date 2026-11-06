"""The sky view's object catalog: how it's built, and the file the page loads."""

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
CATALOG = ROOT / "front/public/ssc/sky_catalog.json"
MAX_BYTES = 150 * 1024

_spec = importlib.util.spec_from_file_location("build_sky_catalog", ROOT / "scripts/build_sky_catalog.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)

COLUMNS = "Name;Type;RA;Dec;MajAx;V-Mag;B-Mag;M;NGC;IC;Common names"
ROWS = [
    "NGC0224;G;00:42:44.35;+41:16:08.6;177.83;3.44;;031;;;Andromeda Galaxy",
    "NGC0001;G;00:07:15.84;+27:42:29.1;1.57;;13.65;;;;",  # faint and small
    "NGC0002;G;00:07:17.11;+27:40:42.0;1.0;15;;;;;Obscure Name",  # named, but cut
    "NGC0100;G;00:24:02.84;-16:29:11.0;4.0;10.5;;;;;",  # bright enough
    "NGC0200;G;00:39:34.0;+02:53:15;12.0;;;;;;",  # no magnitude, but large
    "NGC4038;G;12:01:53.0;-18:52:10;5.2;;;;;;",  # curated, kept for its name alone
    "NGC0281;HII;00:52:59.35;+56:37:18.8;35.00;;;;;0011;",
    "IC0434;HII;05:41:01;-02:27:13;90;;;;;;Flame Nebula,Orion B",
    "NGC1909;RfN;05:04:54;-07:15:58;180;;;;;;the Witch Head Nebula",
    "M102;Dup;14:03:12.54;+54:20:56.2;;;;101;;;",
    "NGC0003;Dup;00:07:16.8;+08:18:06;;;;;;;",
    "IC1318;*;20:22:13.7;+40:15:24;;;;;;;gam Cyg",
    "IC1064;NonEx;;;;;;;;;",
    "M040;**;12:22:16.1;+58:05:04;;8.0;;040;;;",
]
SHARPLESS = [
    "#Coosys\tB1900:\teq_FK4 B1900",
    "recno\tSh2\tRA1900\tDE1900\tDiam",
    " \t \t\"h:m:s\"\t\"d:m:s\"\tarcmin",
    "--------\t----\t----------\t---------\t----",
    "     157\t 157\t23 11 42.0\t+59 30 00\t  90",
    "     184\t 184\t00 47 00.0\t+56 05 00\t  40",
    "     999\t 999\t01 00 00.0\t+10 00 00\t",
]


@pytest.fixture
def ngc(tmp_path):
    path = tmp_path / "NGC.csv"
    path.write_text("\n".join([COLUMNS, *ROWS]) + "\n")
    return path


def _by_name(objects):
    return {o["n"]: o for o in objects}


def _hms(ra_deg):
    hours = ra_deg / 15
    return int(hours), (hours - int(hours)) * 60


def test_b1900_conversion_matches_a_known_case():
    ra, dec = build.b1900_to_j2000("23 11 42.0", "+59 30 00")
    hours, minutes = _hms(ra)
    # Sh2-157 is near 23h16m +60d02m in J2000.
    assert hours == 23 and minutes == pytest.approx(16, abs=0.2)
    assert dec == pytest.approx(60 + 2 / 60, abs=0.02)


def test_conversion_really_precesses():
    ra, dec = build.b1900_to_j2000("00 00 00.0", "+00 00 00")
    # A century of precession moves this point about 1.28 degrees in RA and 0.56 in Dec.
    assert ra == pytest.approx(1.28, abs=0.05)
    assert dec == pytest.approx(0.56, abs=0.05)


@pytest.mark.parametrize(
    "raw, name",
    [("NGC0224", "NGC 224"), ("IC0011", "IC 11"), ("B033", "B 33"), ("Mel022", "Mel 22"), ("ESO056-115", "ESO 056-115")],
)
def test_designations_are_tidied(raw, name):
    assert build.designation(raw) == name


def test_openngc_cuts(ngc):
    names = _by_name(build.read_openngc(ngc))
    assert {"M 31", "NGC 100", "NGC 200", "NGC 281", "M 40", "IC 434", "NGC 1909"} <= set(names)
    for dropped in ("NGC 1", "NGC 2", "NGC 3", "M 102", "IC 1318", "IC 1064"):
        assert dropped not in names


def test_a_curated_name_keeps_a_galaxy_the_cut_would_drop(ngc):
    assert _by_name(build.read_openngc(ngc))["NGC 4038"]["c"] == "Antennae Galaxies"


def test_openngc_names_and_positions(ngc):
    names = _by_name(build.read_openngc(ngc))
    m31 = names["M 31"]
    assert m31["c"] == "Andromeda Galaxy" and m31["a"] == ["NGC 224"]
    assert m31["r"] == pytest.approx(10.685, abs=0.001) and m31["d"] == pytest.approx(41.269, abs=0.001)
    assert names["NGC 1909"]["c"] == "Witch Head Nebula"
    assert names["NGC 281"]["c"] == "Pacman Nebula" and names["NGC 281"]["a"] == ["IC 11"]
    assert names["IC 434"]["c"] == "", "OpenNGC's Flame Nebula on IC 434 is corrected"


def test_sharpless_rows_skip_the_units_and_dashes():
    regions = _by_name(build.read_sharpless(SHARPLESS))
    assert set(regions) == {"Sh2-157", "Sh2-184", "Sh2-999"}
    assert regions["Sh2-157"]["c"] == "Lobster Claw Nebula" and regions["Sh2-157"]["s"] == 90.0
    assert regions["Sh2-999"]["s"] is None


def test_a_matched_region_folds_into_the_kept_object(ngc):
    combined = _by_name(build.combine(build.read_openngc(ngc), build.read_sharpless(SHARPLESS)))
    assert "Sh2-184" not in combined
    assert "Sh2-184" in combined["NGC 281"]["a"]
    assert combined["NGC 281"]["s"] == 40.0, "the larger of the two sizes"
    assert "Sh2-157" in combined


def test_a_match_far_away_on_the_sky_is_refused():
    kept = {"n": "NGC 281", "c": "", "t": "HII", "r": 100.0, "d": 0.0, "s": 35.0, "a": [], "top": False}
    region = {"n": "Sh2-184", "c": "", "t": "HII", "r": 13.0, "d": 56.0, "s": 40.0, "a": [], "top": False}
    with pytest.raises(ValueError, match="too far"):
        build.combine([kept], [region])


def test_a_curated_name_missing_from_the_data_stops_the_build(ngc):
    with pytest.raises(ValueError, match="curated names"):
        build.build(ngc, SHARPLESS)


def test_write_is_json_with_one_object_a_line(tmp_path):
    path = tmp_path / "out.json"
    catalog = {"attribution": "x", "objects": [{"n": "M 1", "t": "SNR", "r": 1.0, "d": 2.0, "l": 0}] * 2}
    build.write(catalog, path)
    assert json.loads(path.read_text()) == catalog
    assert len(path.read_text().splitlines()) == 5


def _shipped():
    return json.loads(CATALOG.read_text())


def test_the_shipped_file_is_small_enough_for_a_phone():
    assert CATALOG.stat().st_size <= MAX_BYTES


def test_the_shipped_file_has_the_right_shape():
    objects = _shipped()["objects"]
    assert len(objects) > 1000
    for o in objects:
        assert set(o) <= {"n", "c", "t", "r", "d", "s", "l", "a"}, o
        assert o["n"] and 0 <= o["r"] < 360 and -90 <= o["d"] <= 90 and o["l"] in (0, 1, 2, 3)
        assert o.get("s", 1) > 0
    names = [o["n"] for o in objects]
    assert len(names) == len(set(names))
    levels = [o["l"] for o in objects]
    assert levels == sorted(levels), "the page relies on priority order"


def test_the_shipped_file_has_every_messier_object_and_no_dropped_types():
    objects = _shipped()["objects"]
    messier = {int(o["n"][2:]) for o in objects if re.fullmatch(r"M \d+", o["n"])}
    assert messier == set(range(1, 111)) - {102}, "OpenNGC lists M 102 as a duplicate of M 101"
    assert all(o["l"] == 0 for o in objects if re.fullmatch(r"M \d+", o["n"]))
    assert not {o["t"] for o in objects} & {"Dup", "*", "NonEx", "Nova"}


@pytest.mark.parametrize(
    "name, common, ra, dec",
    [
        ("Sh2-157", "Lobster Claw Nebula", 349.015, 60.046),
        ("IC 1805", "Heart Nebula", 38.173, 61.457),
        ("IC 1848", "Soul Nebula", 42.794, 60.402),
        ("NGC 281", "Pacman Nebula", 13.247, 56.622),
        ("IC 1396", "Elephant's Trunk Nebula", 324.74, 57.489),
        ("NGC 7000", "North America Nebula", 314.821, 44.529),
    ],
)
def test_the_shipped_file_has_the_popular_objects(name, common, ra, dec):
    found = _by_name(_shipped()["objects"])[name]
    assert found["c"] == common and found["l"] == 0
    assert found["r"] == pytest.approx(ra, abs=0.01) and found["d"] == pytest.approx(dec, abs=0.01)


def test_the_page_can_name_and_draw_every_type():
    script = (ROOT / "front/public/sky_objects.js").read_text()
    table = script[script.index("const TYPES = {"): script.index("const SHAPES = {")]
    shapes = script[script.index("const SHAPES = {"): script.index("function isRed")]
    for kind in {o["t"] for o in _shipped()["objects"]}:
        assert re.search(rf'["\s]{re.escape(kind)}"?: \[', table), kind
    for shape in set(re.findall(r'\["[^"]+", "(\w+)"\]', table)):
        assert re.search(rf"\b{shape}\(ctx", shapes), shape


def test_attribution_sits_with_the_file():
    assert "CC BY-SA 4.0" in _shipped()["attribution"]
    licence = (CATALOG.parent / "sky_catalog-LICENSE.txt").read_text()
    assert "OpenNGC" in licence and "CC BY-SA 4.0" in licence and "VII/20" in licence
