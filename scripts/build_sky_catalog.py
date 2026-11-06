#!/usr/bin/env python
"""Build front/public/ssc/sky_catalog.json, the objects marked on the Targets sky view.

Sources
  OpenNGC, by Mattia Verga, CC BY-SA 4.0: https://github.com/mattiaverga/OpenNGC
    Read from PFRSentinel's star_data/NGC.csv, semi-colon delimited, J2000.
  Sharpless 2 (Sharpless 1959, ApJS 4, 257), VizieR catalogue VII/20:
    https://vizier.cfa.harvard.edu/viz-bin/asu-tsv?-source=VII/20/catalog&-out.max=400&-out.all
    Its coordinates are B1900 (FK4), converted here to J2000 (FK5) with astropy.
  Hand-made common names and matches, below. These are the only editorial data.

What is kept
  Every Messier object. M 102 has no marker: OpenNGC lists it as a duplicate of M 101.
  All nebulae and clusters: see KEPT_TYPES. Dup and * rows are dropped.
  Galaxies at V 11 or brighter and 3' or longer, or 10' or longer at any brightness,
  plus any named in COMMON_NAMES. A 3' galaxy is about 26 pixels across on the S30 Pro's
  2.2 by 4 degree field.
  All of Sharpless 2. Where SAME_OBJECT says a Sharpless region is an object already
  kept, the two become one entry, with the larger of their two sizes.

Output: {"attribution": ..., "objects": [...]}, most worth showing first. Each object has
  n name, t type, r RA in degrees, d Dec in degrees, l level, and if known c common name,
  s major axis in arcminutes, a other designations. Level 0 is shown at any zoom, and
  higher levels appear as the view closes in. It holds Messier, curated names, and other
  named objects except galaxies, whose OpenNGC names are often obscure.

Run from the repo root: python scripts/build_sky_catalog.py
Pass --sharpless FILE to read a saved copy of the VizieR table instead of fetching it.
"""

import argparse
import csv
import json
import re
import urllib.request
from pathlib import Path

import astropy.units as u
from astropy.coordinates import FK4, FK5, Angle, SkyCoord

SHARPLESS_URL = "https://vizier.cfa.harvard.edu/viz-bin/asu-tsv?-source=VII/20/catalog&-out.max=400&-out.all"
DEFAULT_NGC = Path.home() / "Astrophotography/PFRSentinel/star_data/NGC.csv"
DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "front/public/ssc/sky_catalog.json"
ATTRIBUTION = (
    "OpenNGC by Mattia Verga, CC BY-SA 4.0, https://github.com/mattiaverga/OpenNGC. "
    "Sharpless 2 (Sharpless 1959), VizieR VII/20, converted from B1900 to J2000. "
    "Selected and renamed by scripts/build_sky_catalog.py."
)

KEPT_TYPES = {"Neb", "EmN", "HII", "RfN", "SNR", "PN", "OCl", "GCl", "Cl+N", "DrkN", "*Ass"}
GALAXY_TYPES = {"G", "GPair", "GTrpl", "GGroup"}

GALAXY_MAX_MAG = 11.0
GALAXY_MIN_SIZE = 3.0
GALAXY_ANY_MAG_SIZE = 10.0
# B minus V for a typical galaxy, for the many rows that have only a B magnitude.
TYPICAL_B_MINUS_V = 0.7

# Major axis in arcminutes for levels 1 and 2. Everything smaller is level 3.
LEVEL_1_SIZE = 30.0
LEVEL_2_SIZE = 10.0

# Popular names the data lacks or words badly. Only names in common use.
COMMON_NAMES = {
    "IC 1805": "Heart Nebula",
    "IC 1848": "Soul Nebula",
    "Sh2-157": "Lobster Claw Nebula",
    "NGC 281": "Pacman Nebula",
    # The trunk itself is IC 1396A, inside this region.
    "IC 1396": "Elephant's Trunk Nebula",
    "NGC 7000": "North America Nebula",
    "M 8": "Lagoon Nebula",
    "M 24": "Small Sagittarius Star Cloud",
    "M 11": "Wild Duck Cluster",
    "M 16": "Eagle Nebula",
    "M 17": "Omega Nebula",
    "M 27": "Dumbbell Nebula",
    "M 31": "Andromeda Galaxy",
    "M 33": "Triangulum Galaxy",
    "M 42": "Orion Nebula",
    "M 43": "De Mairan's Nebula",
    "M 44": "Beehive Cluster",
    "M 51": "Whirlpool Galaxy",
    "M 63": "Sunflower Galaxy",
    "M 64": "Black Eye Galaxy",
    "M 74": "Phantom Galaxy",
    "M 76": "Little Dumbbell Nebula",
    "M 81": "Bode's Galaxy",
    "M 82": "Cigar Galaxy",
    "M 83": "Southern Pinwheel Galaxy",
    "M 87": "Virgo A",
    "M 97": "Owl Nebula",
    "M 101": "Pinwheel Galaxy",
    "M 104": "Sombrero Galaxy",
    "IC 410": "Tadpoles Nebula",
    "IC 443": "Jellyfish Nebula",
    "IC 63": "Ghost of Cassiopeia",
    "IC 348": "Omicron Persei Cloud",
    "IC 1795": "Fish Head Nebula",
    "IC 2177": "Seagull Nebula",
    "IC 2391": "Omicron Velorum Cluster",
    "IC 2602": "Southern Pleiades",
    "IC 2944": "Running Chicken Nebula",
    "IC 4592": "Blue Horsehead Nebula",
    "IC 4604": "Rho Ophiuchi Nebula",
    "HCG 79": "Seyfert's Sextet",
    "HCG 92": "Stephan's Quintet",
    "NGC 40": "Bow-Tie Nebula",
    "NGC 104": "47 Tucanae",
    "NGC 253": "Sculptor Galaxy",
    "NGC 2024": "Flame Nebula",
    "NGC 2070": "Tarantula Nebula",
    "NGC 2237": "Rosette Nebula",
    "NGC 2359": "Thor's Helmet",
    "NGC 3628": "Hamburger Galaxy",
    "NGC 4038": "Antennae Galaxies",
    "NGC 4565": "Needle Galaxy",
    "NGC 4631": "Whale Galaxy",
    "NGC 4676": "Mice Galaxies",
    "NGC 5128": "Centaurus A",
    "NGC 5907": "Splinter Galaxy",
    "NGC 6087": "S Normae Cluster",
    "NGC 6334": "Cat's Paw Nebula",
    "NGC 6946": "Fireworks Galaxy",
    "NGC 6995": "Bat Nebula",
    "NGC 7380": "Wizard Nebula",
    "NGC 7662": "Blue Snowball Nebula",
    "Sh2-27": "Zeta Ophiuchi Nebula",
    "Sh2-82": "Little Cocoon Nebula",
    "Sh2-101": "Tulip Nebula",
    "Sh2-103": "Cygnus Loop",
    "Sh2-108": "Sadr Region",
    "Sh2-119": "Clamshell Nebula",
    "Sh2-129": "Flying Bat Nebula",
    "Sh2-132": "Lion Nebula",
    "Sh2-136": "Ghost Nebula",
    "Sh2-170": "Little Rosette Nebula",
    "Sh2-240": "Spaghetti Nebula",
    "Sh2-261": "Lower's Nebula",
    "Sh2-264": "Lambda Orionis Ring",
    "Sh2-276": "Barnard's Loop",
    "Sh2-308": "Dolphin Head Nebula",
}

# OpenNGC calls IC 434 the Flame Nebula, which is NGC 2024 beside it. IC 434 is the glow
# behind the Horsehead, and has no separate popular name.
WRONG_NAMES = {"IC 434"}

# A Sharpless region that is an object already kept. Checked on the sky by same_object_is_close().
SAME_OBJECT = {
    "Sh2-8": "NGC 6334",
    "Sh2-11": "NGC 6357",
    "Sh2-25": "M 8",
    "Sh2-30": "M 20",
    "Sh2-45": "M 17",
    "Sh2-49": "M 16",
    "Sh2-105": "NGC 6888",
    "Sh2-117": "NGC 7000",
    "Sh2-125": "IC 5146",
    "Sh2-131": "IC 1396",
    "Sh2-142": "NGC 7380",
    "Sh2-155": "C 9",
    "Sh2-162": "NGC 7635",
    "Sh2-184": "NGC 281",
    "Sh2-190": "IC 1805",
    "Sh2-199": "IC 1848",
    "Sh2-220": "NGC 1499",
    "Sh2-229": "IC 405",
    "Sh2-236": "IC 410",
    "Sh2-244": "M 1",
    "Sh2-248": "IC 443",
    "Sh2-252": "NGC 2174",
    "Sh2-273": "NGC 2264",
    "Sh2-275": "NGC 2237",
    "Sh2-281": "M 42",
}

# Sharpless is a list of H II regions, and these are known to be something else.
SHARPLESS_TYPES = {"Sh2-103": "SNR"}


def number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def designation(raw):
    """NGC0224 to NGC 224, B033 to B 33. Names with a dash, like ESO056-115, keep their digits."""
    match = re.fullmatch(r"([A-Za-z]+)0*(\d+)(.*)", raw.strip())
    if not match or "-" in match.group(3):
        match = re.fullmatch(r"([A-Za-z]+)(\d.*)", raw.strip())
        return f"{match.group(1)} {match.group(2)}" if match else raw.strip()
    return f"{match.group(1)} {match.group(2)}{match.group(3)}"


def b1900_to_j2000(ra_text, dec_text):
    """(RA, Dec) in degrees, J2000, from sexagesimal B1900 FK4 such as "23 11 42.0", "+59 30 00"."""
    position = SkyCoord(ra_text, dec_text, unit=(u.hourangle, u.deg), frame=FK4(equinox="B1900"))
    j2000 = position.transform_to(FK5(equinox="J2000"))
    return float(j2000.ra.deg), float(j2000.dec.deg)


def _common_name(row, name):
    if name in WRONG_NAMES:
        return ""
    first = row["Common names"].split(",")[0].strip()
    return re.sub(r"^the\s+", "", first, flags=re.I)


def _keeps_galaxy(row, size):
    mag = number(row["V-Mag"])
    if mag is None:
        blue = number(row["B-Mag"])
        mag = blue - TYPICAL_B_MINUS_V if blue is not None else None
    if size is not None and size >= GALAXY_ANY_MAG_SIZE:
        return True
    return mag is not None and mag <= GALAXY_MAX_MAG and size is not None and size >= GALAXY_MIN_SIZE


def _others(row, plain):
    others = [plain] if plain else []
    for column, prefix in (("NGC", "NGC"), ("IC", "IC")):
        for part in filter(None, (p.strip() for p in row[column].split(","))):
            others.append(designation(f"{prefix}{part}"))
    return others


def read_openngc(path):
    objects = []
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle, delimiter=";"):
            kind = row["Type"]
            messier = row["M"].strip()
            if kind == "Dup" or not row["RA"] or not row["Dec"]:
                continue
            plain = designation(row["Name"])
            name = f"M {int(messier)}" if messier else plain
            curated = COMMON_NAMES.get(name) or COMMON_NAMES.get(plain)
            size = number(row["MajAx"])
            galaxy = kind in GALAXY_TYPES
            if not (messier or curated or kind in KEPT_TYPES or (galaxy and _keeps_galaxy(row, size))):
                continue
            objects.append(
                {
                    "n": name,
                    "c": curated or _common_name(row, plain),
                    "t": kind,
                    "r": Angle(row["RA"], unit=u.hourangle).deg,
                    "d": Angle(row["Dec"], unit=u.deg).deg,
                    "s": size,
                    "a": _others(row, plain if name != plain else ""),
                    "top": bool(messier or curated) or (not galaxy and bool(_common_name(row, plain))),
                }
            )
    return objects


def read_sharpless(lines):
    """Rows of the VizieR table: comments, then a header, its units, and a line of dashes."""
    columns = None
    objects = []
    for line in lines:
        if line.startswith("#") or not line.strip():
            continue
        cells = [cell.strip() for cell in line.rstrip("\n").split("\t")]
        if columns is None:
            columns = cells
            continue
        row = dict(zip(columns, cells))
        if not row.get("Sh2", "").isdigit():
            continue  # the units line and the dashes
        ra, dec = b1900_to_j2000(row["RA1900"], row["DE1900"])
        name = f"Sh2-{int(row['Sh2'])}"
        objects.append(
            {
                "n": name,
                "c": COMMON_NAMES.get(name, ""),
                "t": SHARPLESS_TYPES.get(name, "HII"),
                "r": ra,
                "d": dec,
                "s": number(row.get("Diam")),
                "a": [],
                "top": name in COMMON_NAMES,
            }
        )
    return objects


def same_object_is_close(region, kept):
    """A match should have the two centres within the larger of the objects, or a degree."""
    here = SkyCoord(region["r"], region["d"], unit=u.deg)
    there = SkyCoord(kept["r"], kept["d"], unit=u.deg)
    limit = max(region["s"] or 0, kept["s"] or 0, 60.0)
    return here.separation(there).arcmin <= limit


def combine(openngc, sharpless):
    by_name = {o["n"]: o for o in openngc}
    combined = list(openngc)
    for region in sharpless:
        match = SAME_OBJECT.get(region["n"])
        if match is None:
            combined.append(region)
            continue
        kept = by_name[match]
        if not same_object_is_close(region, kept):
            raise ValueError(f"{region['n']} is too far from {match} to be the same object")
        kept["a"].append(region["n"])
        kept["s"] = max(kept["s"] or 0, region["s"] or 0) or None
    return combined


def level(obj):
    if obj["top"]:
        return 0
    size = obj["s"] or 0
    return 1 if size >= LEVEL_1_SIZE else 2 if size >= LEVEL_2_SIZE else 3


def _compact(obj):
    entry = {"n": obj["n"], "t": obj["t"], "r": round(obj["r"], 3), "d": round(obj["d"], 3), "l": level(obj)}
    if obj["c"]:
        entry["c"] = obj["c"]
    if obj["s"]:
        # Planetary nebulae can be a few arcseconds, which one decimal would round to nothing.
        entry["s"] = round(obj["s"], 1 if obj["s"] >= 1 else 2)
        if entry["s"] == int(entry["s"]):
            entry["s"] = int(entry["s"])
    if obj["a"]:
        entry["a"] = obj["a"]
    return entry


def build(ngc_path, sharpless_lines):
    openngc = read_openngc(ngc_path)
    sharpless = read_sharpless(sharpless_lines)
    names = {o["n"] for o in openngc + sharpless} | {other for o in openngc for other in o["a"]}
    missing = (set(COMMON_NAMES) | set(SAME_OBJECT) | set(SAME_OBJECT.values())) - names
    if missing:
        raise ValueError(f"curated names not in the data: {sorted(missing)}")
    objects = combine(openngc, sharpless)
    objects.sort(key=lambda o: (level(o), -(o["s"] or 0), o["n"]))
    return {"attribution": ATTRIBUTION, "objects": [_compact(o) for o in objects]}


def write(catalog, path):
    # One object a line: barely larger than a single line, and it diffs well.
    lines = ",\n".join(json.dumps(o, ensure_ascii=False, separators=(",", ":")) for o in catalog["objects"])
    text = f'{{"attribution":{json.dumps(catalog["attribution"])},\n"objects":[\n{lines}\n]}}\n'
    Path(path).write_text(text, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--ngc", default=DEFAULT_NGC, help="OpenNGC's NGC.csv")
    parser.add_argument("--sharpless", help="a saved copy of the VizieR table (default: fetch it)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.sharpless:
        lines = Path(args.sharpless).read_text(encoding="utf-8").splitlines()
    else:
        with urllib.request.urlopen(SHARPLESS_URL, timeout=60) as reply:
            lines = reply.read().decode("utf-8").splitlines()
    catalog = build(args.ngc, lines)
    write(catalog, args.output)
    counts = [sum(1 for o in catalog["objects"] if o["l"] == n) for n in range(4)]
    print(f"{len(catalog['objects'])} objects, levels {counts}, {Path(args.output).stat().st_size} bytes")


if __name__ == "__main__":
    main()
