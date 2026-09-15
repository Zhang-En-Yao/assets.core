#!/usr/bin/env python3
"""Build countries-110m.json from Natural Earth's own 1:110m admin-0 countries — the
coarsest of Natural Earth's three standard tiers, already generalized by their
cartographers rather than mechanically decimated from a finer one.

Same source, filtering and degenerate-ring handling as build-atlas.py's 1:10m build
(see there for why `solid()`/`ring_area()` exist — a few three-vertex marine slivers
elsewhere in Natural Earth data have reversed winding once quantized, though the 110m
admin-0 countries layer hasn't shown the problem; kept for parity and safety). The only
difference is the tool building the topology: build-atlas.py shells out to the
topojson-server/-simplify/-client npm packages via npx, which needs node; this uses the
`topojson` PyPI package instead, since 110m is already small enough that no additional
simplification pass is needed, only the shared-arc encoding and quantization.

    pip install topojson
    python3 build/build-atlas-110m.py
"""

import json
import pathlib
import urllib.request

import topojson as tp

HERE = pathlib.Path(__file__).parent
BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson"
QUANTIZE = 1e5  # matches build-atlas.py: ~400 m on a world bounding box


def fetch(name):
    print(f"  {name}")
    with urllib.request.urlopen(f"{BASE}/{name}.geojson", timeout=180) as response:
        return json.load(response)


MIN_RING_DEG2 = 1e-5


def ring_area(ring):
    ox, oy = ring[0]
    total = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        total += (x1 - ox) * (y2 - oy) - (x2 - ox) * (y1 - oy)
    return abs(total) / 2


def solid(geometry):
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


def features(source, keep):
    out = []
    for feature in source["features"]:
        props = feature["properties"]
        if not feature.get("geometry"):
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


def main():
    print("downloading Natural Earth 1:110m")
    countries = features(fetch("ne_110m_admin_0_countries"), {"name": "NAME"})
    print(f"{len(countries['features'])} countries")

    topo = tp.Topology(countries, object_name="countries", prequantize=QUANTIZE, topology=True)
    out = HERE.parent / "atlas" / "countries-110m.json"
    out.write_text(topo.to_json(), encoding="utf-8")
    print(f"{out.name}: {out.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
