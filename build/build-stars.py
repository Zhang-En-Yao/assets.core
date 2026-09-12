#!/usr/bin/env python3
"""Regenerate stars.json — the real sky drawn in the world map's empty polar bands.

The travel map is square, but the projected world is about 2:1, so a band of about a
quarter of the height is left over above the Arctic and below Antarctica. Rather than
invent something to put there, those bands carry an actual star chart, centred on the
north and south celestial poles.

Two catalogues supply the chart:

  AT-HYG v4.0                 the stars, from its Codeberg home (astronexus, CC BY-SA 4.0)
  constellations.lines.json   the IAU constellation figures from d3-celestial

AT-HYG is the Augmented Tycho-HYG database: Hipparcos, Tycho-2 and Gliese merged, then
augmented from Gaia DR3. It is the successor to plain HYG, which carried no Tycho-2 and no
Gaia. The subset used here, hyglike_from_athyg, keeps the HYG column names, so the CSV
reads the same way HYG always did.

Be clear about what Gaia does and does not give this chart. Measured over the subset:
above magnitude 6, positions and magnitudes come from Tycho-2 for 100% of stars, while
93% of the distances come from Gaia DR3. Gaia's cameras saturate around G = 3, so it has
no reliable astrometry for the stars that draw the constellations, and AT-HYG falls back
to Tycho-2 and Hipparcos for them. A flat sky chart uses right ascension, declination and
magnitude only — so the chart is drawn from Tycho-2, and Gaia contributes nothing visible
until something here needs distance. The build prints the breakdown on every run.

Right ascension is in hours in the catalogue, so it is converted to degrees (and wrapped
to the [-180, 180] range used by d3-geo). Sol is kept, at id 0: the catalogue files it at
0h, 0° — the vernal equinox, where the Sun stands each March — and world-map.js gives it a
radius on the same soft-capped curve as every other star.

Trimmed to magnitude 6 — the naked-eye limit under a dark sky. Each band spans a whole
hemisphere, so anything brighter leaves the corners looking empty. The limit is written
into the file as magMax; world-map.js reads it back to size and fade each star.

Run from the repo root when you want a different magnitude limit:

    python3 assets/data/build-stars.py
"""

import collections
import csv
import gzip
import io
import json
import pathlib
import urllib.request

HERE = pathlib.Path(__file__).parent
OUT = HERE / "stars.json"

# Codeberg's media endpoint resolves the Git LFS pointer to the real file (14 MB gzipped).
ATHYG_URL = (
    "https://codeberg.org/astronexus/athyg/media/branch/main/"
    "data/subsets/hyglike_from_athyg_v40.csv.gz"
)
# Pinned release first, with the d3-celestial repository as a fallback.
CELESTIAL_BASES = (
    "https://cdn.jsdelivr.net/npm/d3-celestial@0.7.35/data",
    "https://raw.githubusercontent.com/ofrohn/d3-celestial/master/data",
)

MAG_MAX = 6.0

# AT-HYG's own codes for where a value came from, spelled out for the build log.
SOURCES = {
    "G_R3": "Gaia DR3", "G_R2": "Gaia DR2", "T": "Tycho-2", "T_X": "Tycho-2 (extended)",
    "HIP": "Hipparcos", "HIP_X": "Hipparcos (extended)", "H": "Hipparcos",
    "GJ": "Gliese", "YBS": "Yale Bright Star", "OTHER": "other", "none": "unrecorded",
}


def fetch_celestial(name):
    for base in CELESTIAL_BASES:
        try:
            with urllib.request.urlopen(f"{base}/{name}", timeout=30) as response:
                return json.load(response)
        except OSError as error:
            print(f"  {base} unreachable ({error})")
    raise SystemExit(f"could not download {name}")


def fetch_athyg():
    try:
        with urllib.request.urlopen(ATHYG_URL, timeout=180) as response:
            with gzip.GzipFile(fileobj=io.BytesIO(response.read())) as archive:
                return list(csv.DictReader(io.TextIOWrapper(archive, encoding="utf-8")))
    except OSError as error:
        raise SystemExit(f"could not download AT-HYG v4.0 ({error})") from error


def main():
    rows = fetch_athyg()
    print(f"AT-HYG v4.0: {len(rows)} stars")

    stars = []
    provenance, distances = collections.Counter(), collections.Counter()
    for star in rows:
        try:
            mag = float(star["mag"])
            ra = float(star["ra"]) * 15
            dec = float(star["dec"])
        except (TypeError, ValueError):
            continue
        if mag > MAG_MAX:
            continue
        if ra > 180:
            ra -= 360
        # 2 decimals is 36 arcseconds, which is a twentieth of a pixel on the drawn chart.
        stars.append([round(ra, 2), round(dec, 2), round(mag, 1)])
        provenance[star.get("pos_src") or "none"] += 1
        distances[star.get("dist_src") or "none"] += 1

    # Faintest first, so the brightest stars are painted last and sit on top.
    stars.sort(key=lambda s: -s[2])

    lines = []
    for feature in fetch_celestial("constellations.lines.json")["features"]:
        paths = [[[round(ra, 2), round(dec, 2)] for ra, dec in segment]
                 for segment in feature["geometry"]["coordinates"]]
        if paths:
            lines.append({"id": feature["id"], "paths": paths})

    OUT.write_text(json.dumps({
        "magMax": MAG_MAX,
        "source": "AT-HYG v4.0 (astronexus, CC BY-SA 4.0) — Hipparcos, Tycho-2 and Gliese"
                  " re-derived against Gaia DR3; constellation lines: d3-celestial"
                  " (Olaf Frohn, BSD-3-Clause)",
        "stars": stars,
        "lines": lines,
    }, separators=(",", ":")) + "\n")

    print(f"{OUT.name}: {len(stars)} stars to magnitude {MAG_MAX}, "
          f"{len(lines)} constellation figures, {OUT.stat().st_size / 1024:.0f} KB")
    for field, tally in (("position", provenance), ("distance", distances)):
        print(f"{field} from:")
        for code, count in tally.most_common():
            print(f"  {SOURCES.get(code, code):22} {count:5d}  {count / len(stars):5.1%}")


if __name__ == "__main__":
    main()
