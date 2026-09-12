#!/usr/bin/env python3
"""Regenerate continents.json — which continent each country in the atlas belongs to.

The atlas (countries-50m.json) carries a country's name and nothing else, but the travel
map needs to group countries by continent to zoom to one. This joins the two by name
against Natural Earth, which is where the atlas itself comes from, so the names line up.

Run when the atlas changes:

    python3 build/build-continents.py
"""

import json
import pathlib
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
ATLAS = REPO / "atlas" / "countries-50m.json"
OUT = REPO / "atlas" / "continents.json"
NE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_0_countries.geojson"

# The atlas kept the old name; Natural Earth has since renamed the country.
RENAME = {"Macedonia": "North Macedonia"}

# Natural Earth files a handful of islands under the continent "Seven seas (open ocean)",
# which is not a continent and not somewhere a reader can click to. These are not all
# Antarctic rocks — the Maldives and Mauritius are here too — so each is placed by hand
# rather than swept into one bucket.
SEVEN_SEAS = {
    "Maldives": "Asia",
    "Seychelles": "Africa",
    "Mauritius": "Africa",
    "Saint Helena": "Africa",
    "Br. Indian Ocean Ter.": "Africa",
    "Fr. S. Antarctic Lands": "Antarctica",
    "Heard I. and McDonald Is.": "Antarctica",
    "S. Geo. and the Is.": "Antarctica",
}


def main():
    atlas = json.loads(ATLAS.read_text())
    names = [g["properties"]["name"] for g in atlas["objects"]["countries"]["geometries"]]

    with urllib.request.urlopen(NE) as r:
        ne = json.load(r)
    continent = {f["properties"]["NAME"]: f["properties"]["CONTINENT"] for f in ne["features"]}

    out = {}
    for name in sorted(names):
        c = SEVEN_SEAS.get(name) or continent.get(RENAME.get(name, name))
        if not c or c == "Seven seas (open ocean)":
            raise SystemExit(f"no continent for {name!r} — add it to SEVEN_SEAS")
        out[name] = c

    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"{OUT.name}: {len(out)} countries")


if __name__ == "__main__":
    main()
