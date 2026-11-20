"""Look an object's coordinates up by name, for the /simbad endpoint.

SIMBAD's own pages first, then CDS's Sesame name resolver, which answers from
SIMBAD's data on mirrors of its own. On 2026-11-19 the CDS SIMBAD host returned a
"page unavailable" 404 for every name and the Harvard mirror timed out, so no
target could be searched; Sesame still resolved them.

A lookup returns "03h46m38.0s +24d10m41.0s on", the coordinates and whether the LP
filter suits the object, or None when no source knows the name.
"""

import re
import threading
import time
import urllib.parse

import requests

# Tried in order. A reply without coordinates moves on to the next, as a dead host does.
SIMBAD_URLS = (
    "https://simbad.cds.unistra.fr/simbad/sim-id?output.format=ASCII&obj.bibsel=off&Ident=",
    "https://simbad.harvard.edu/simbad/sim-id?output.format=ASCII&obj.bibsel=off&Ident=",
)
# Sesame, SIMBAD only (S), plain text with object type (-oI). The CDS address forwards
# to whichever mirror is up; the others are tried should it be down itself.
SESAME_URLS = (
    "https://cds.unistra.fr/cgi-bin/nph-sesame/-oI/S?",
    "https://vizier.cfa.harvard.edu/viz-bin/nph-sesame/-oI/S?",
    "https://vizier.idia.ac.za/viz-bin/nph-sesame/-oI/S?",
)
TIMEOUT_S = 5
# A source that failed is passed over this long, so each search during an outage
# doesn't wait out a dead host's timeout first (Harvard's alone took 5 s a search).
RESTING_S = 600

_resting = {}  # source URL -> when to try it again
_resting_lock = threading.Lock()

_SIMBAD_COORDS = "Coordinates(ICRS,ep=J2000,eq=2000):"
# SIMBAD's ASCII page lists the object's place in its type hierarchy. These branches
# glow in the lines the LP filter passes.
_SIMBAD_LP_BRANCHES = ("---  ISM  ---", "---  HII  ---", "---  SNR  ---", "---  PN  ---")
# The same branches as Sesame's short type codes: the interstellar medium (clouds,
# nebulae, HII regions, bubbles, star-forming regions), supernova remnants and planetary
# nebulae. A trailing "?" (candidate) counts as the type.
_SESAME_LP_TYPES = {
    "ISM", "Cld", "GNe", "DNe", "RNe", "MoC", "glb", "cor", "SFR", "HVC", "HII",
    "SNR", "bub", "sh", "EmO", "PN", "out", "HH", "of",
}  # fmt: skip


def lookup(name: str, get=requests.get, clock=time.monotonic) -> str | None:
    """Coordinates and LP advice for a name, or None if no source knows it.

    A source resting after a failure is skipped, unless every source is resting: then
    they're all tried, as the alternative is not looking at all.
    """
    name = (name or "").strip()
    if not name:
        return None
    sources = (
        (SIMBAD_URLS, urllib.parse.quote_plus, _from_simbad),
        (SESAME_URLS, urllib.parse.quote, _from_sesame),
    )
    now = clock()
    every = [url for urls, _, _ in sources for url in urls]
    awake = {url for url in every if not _is_resting(url, now)} or set(every)
    for urls, quote, parse in sources:
        for url in (url for url in urls if url in awake):
            text = _fetch(get, url + quote(name))
            if text is None:
                _rest(url, clock())
                continue
            _wake(url)
            found = parse(text)
            if found:
                return found
    return None


def _is_resting(url, now):
    with _resting_lock:
        return _resting.get(url, 0) > now


def _rest(url, now):
    with _resting_lock:
        _resting[url] = now + RESTING_S


def _wake(url):
    with _resting_lock:
        _resting.pop(url, None)


def _fetch(get, url):
    """The page's text, or None if the source is down: no answer, or not a 200."""
    try:
        reply = get(url, timeout=TIMEOUT_S)
    except Exception:
        return None
    if getattr(reply, "status_code", 200) != 200:
        return None
    return reply.text


def _from_simbad(text: str) -> str | None:
    start = text.find(_SIMBAD_COORDS)
    if start < 0:
        return None
    end = text.find("(", start + 13)  # past the "(" of "Coordinates("
    parts = re.split(r"\s+", text[start + len(_SIMBAD_COORDS) : end].strip())
    if len(parts) < 6:
        return None
    try:
        ra_s, dec_s = round(float(parts[2]), 1), round(float(parts[5]), 1)
    except ValueError:
        return None
    coords = f"{parts[0]}h{parts[1]}m{ra_s}s {parts[3]}d{parts[4]}m{dec_s}s"
    lp = any(branch in text for branch in _SIMBAD_LP_BRANCHES)
    return f"{coords} {'on' if lp else 'off'}"


def _from_sesame(text: str) -> str | None:
    coords = otype = None
    for line in text.splitlines():
        if coords is None and line.startswith("%J "):
            fields = line.split()
            try:
                coords = float(fields[1]), float(fields[2])
            except (IndexError, ValueError):
                return None
        elif otype is None and line.startswith("%C"):
            fields = line.split()
            otype = fields[1] if len(fields) > 1 else ""
    if coords is None:
        return None
    lp = (otype or "").rstrip("?") in _SESAME_LP_TYPES
    return f"{_hms(coords[0])} {_dms(coords[1])} {'on' if lp else 'off'}"


def _hms(ra_deg: float) -> str:
    """RA in degrees as SIMBAD's page gives it: 03h46m38.0s."""
    tenths = round((ra_deg % 360) / 15 * 36000)  # tenths of a second of time
    tenths %= 24 * 36000
    h, rest = divmod(tenths, 36000)
    m, s10 = divmod(rest, 600)
    return f"{h:02d}h{m:02d}m{s10 / 10:.1f}s"


def _dms(dec_deg: float) -> str:
    """Dec in degrees as SIMBAD's page gives it: +24d10m41.0s."""
    sign = "-" if dec_deg < 0 else "+"
    tenths = round(abs(dec_deg) * 36000)  # tenths of an arcsecond
    d, rest = divmod(tenths, 36000)
    m, s10 = divmod(rest, 600)
    return f"{sign}{d:02d}d{m:02d}m{s10 / 10:.1f}s"
