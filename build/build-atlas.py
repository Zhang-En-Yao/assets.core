#!/usr/bin/env python3
"""Regenerate countries-10m.json and marine-areas.json from Natural Earth 1:10m.

Three layers, straight from the Natural Earth vector repository, with nothing changed but
the property list (trimmed to the fields the map reads) and the encoding:

  ne_10m_admin_0_countries                        → countries-10m.json, object "countries"
  ne_10m_geography_marine_polys                   → marine-areas.json, object "areas"
  ne_10m_admin_0_boundary_lines_maritime_indicator → marine-areas.json, object "borders"

GeoJSON at this resolution is 15 MB; TopoJSON stores each shared border once and
delta-encodes it, which is what makes 1:10m affordable on a static site. Simplification
drops the smallest triangles until half the vertices are gone — at the map's deepest zoom
one pixel is about a kilometre, so what goes was never visible. countries-50m.json stays
in the repo: the map paints that first and only fetches this when someone zooms in.

Needs node for the TopoJSON tools, which are fetched by npx on first run:

    python3 assets/data/build-atlas.py
    python3 assets/data/build-atlas.py --keep 0.7   # keep more vertices
"""

import argparse
import json
import pathlib
import shutil
import subprocess
import tempfile
import urllib.request

HERE = pathlib.Path(__file__).parent
BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson"

# Natural Earth marine polygons cover far more than named waters; these are the ones the
# map draws as divisions of the sea.
MARINE_TYPES = {"ocean", "sea", "gulf", "bay", "strait", "channel", "sound"}

QUANTIZE = "1e5"  # ~400 m on a world bounding box, about a third of a pixel at full zoom
NPX = ["npx", "-y", "-p", "topojson-server", "-p", "topojson-simplify", "-p", "topojson-client"]


def fetch(name):
    print(f"  {name}")
    with urllib.request.urlopen(f"{BASE}/{name}.geojson", timeout=180) as response:
        return json.load(response)


# Natural Earth's marine polygons include a few three-vertex slivers with no real area.
# Quantizing rounds their corners onto the same grid line, which can reverse the winding,
# and d3 reads a reversed ring as the whole globe — one such sliver in the Yucatan Channel
# drew an outline around the entire map. Anything under a square kilometre is smaller than
# a pixel at the deepest zoom, so dropping it costs nothing.
MIN_RING_DEG2 = 1e-5


def ring_area(ring):
    """Shoelace area in square degrees, shifted to the ring's own first vertex so a sliver
    a millionth of a degree across is not lost to floating-point cancellation."""
    ox, oy = ring[0]
    total = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        total += (x1 - ox) * (y2 - oy) - (x2 - ox) * (y1 - oy)
    return abs(total) / 2


def solid(geometry):
    """The geometry without its degenerate rings, or None if nothing is left."""
    kind = geometry["type"]
    if kind not in ("Polygon", "MultiPolygon"):
        return geometry
    polygons = geometry["coordinates"] if kind == "MultiPolygon" else [geometry["coordinates"]]
    kept = []
    for polygon in polygons:
        rings = [r for r in polygon if len(r) >= 4 and ring_area(r) >= MIN_RING_DEG2]
        if rings:
            kept.append(rings)
    if not kept:
        return None
    return {"type": kind, "coordinates": kept if kind == "MultiPolygon" else kept[0]}


def features(source, keep, where=lambda props: True):
    """The source's features, carrying only the properties `keep` names, under those names."""
    out = []
    for feature in source["features"]:
        props = feature["properties"]
        if not feature.get("geometry") or not where(props):
            continue
        geometry = solid(feature["geometry"])
        if geometry is None:
            continue
        out.append({
            "type": "Feature",
            "properties": {out_key: props[src_key] for out_key, src_key in keep.items()},
            "geometry": geometry,
        })
    return {"type": "FeatureCollection", "features": out}


def topojson(out, layers, simplify=None):
    """geo2topo | toposimplify | topoquantize, the standard TopoJSON pipeline."""
    if not shutil.which("npx"):
        raise SystemExit("npx not found — install node, or run this where node is available")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        args = []
        for name, collection in layers.items():
            path = tmp / f"{name}.geojson"
            path.write_text(json.dumps(collection, separators=(",", ":")))
            args.append(f"{name}={path}")
        step = subprocess.run(NPX + ["geo2topo"] + args, capture_output=True, check=True)
        data = step.stdout
        if simplify:
            data = subprocess.run(NPX + ["toposimplify", "-p", str(simplify)],
                                  input=data, capture_output=True, check=True).stdout
        data = subprocess.run(NPX + ["topoquantize", QUANTIZE],
                              input=data, capture_output=True, check=True).stdout
    out.write_bytes(data)
    objects = ", ".join(layers)
    print(f"{out.name}: {out.stat().st_size / 1024:.0f} KB ({objects})")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", type=float, default=0.5,
                        help="share of vertices to keep in the country outlines (default 0.5)")
    args = parser.parse_args()

    print("downloading Natural Earth 1:10m")
    countries = features(fetch("ne_10m_admin_0_countries"), {"name": "NAME"})
    marine = features(fetch("ne_10m_geography_marine_polys"),
                      {"name": "name", "type": "featurecla", "rank": "scalerank"},
                      where=lambda p: p["featurecla"] in MARINE_TYPES)
    maritime = features(fetch("ne_10m_admin_0_boundary_lines_maritime_indicator"), {})

    print(f"{len(countries['features'])} countries, {len(marine['features'])} named waters, "
          f"{len(maritime['features'])} maritime boundaries")
    topojson(HERE / "countries-10m.json", {"countries": countries}, simplify=args.keep)
    topojson(HERE / "marine-areas.json", {"areas": marine, "borders": maritime})


if __name__ == "__main__":
    main()
