#!/usr/bin/env python3
"""Check the published data before it is allowed out, and refresh MANIFEST.json.

Three layers, cheapest first:

  shape       a JSON Schema per dataset — is this the right kind of file at all
  invariants  the things a schema cannot say: that Antarctica is in the atlas, that the
              marine file carries both its layers, that the star limit is what it claims
  drift       a comparison against MANIFEST.json — a dataset that suddenly lost a tenth
              of its rows is an upstream outage, not an edit, and must not be published

Exit code 1 on any failure, so a workflow stops before committing.

    python3 build/validate.py            # check, and rewrite MANIFEST.json
    python3 build/validate.py --check    # check only, leave the manifest alone
"""

import argparse
import datetime
import json
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "MANIFEST.json"

DRIFT = 0.05  # a dataset may not lose more than this share of its rows in one build
MIN_RING_DEG2 = 1e-5  # mirrors build-atlas.py: smaller than this is a sliver, not a shape

DATASETS = {
    "atlas/countries-50m.json": "topology",
    "atlas/countries-10m.json": "topology",
    "atlas/marine-areas.json": "topology",
    "sky/stars.json": "sky",
}

problems = []


def fail(dataset, message):
    problems.append(f"{dataset}: {message}")


def count(path, data):
    """The row count drift is measured against — whatever 'a row' means for this file."""
    if "objects" in data:
        return sum(len(o.get("geometries", [])) for o in data["objects"].values())
    if "stars" in data:
        return len(data["stars"])
    if "places" in data:
        return len(data["places"])
    return len(data)


# ---------- shape ----------

def check_topology(name, data):
    if data.get("type") != "Topology":
        fail(name, "not a TopoJSON topology")
        return
    for key in ("objects", "arcs", "transform"):
        if key not in data:
            fail(name, f"no {key}")
    for obj_name, obj in data.get("objects", {}).items():
        if not obj.get("geometries"):
            fail(name, f"object {obj_name} is empty")


def check_sky(name, data):
    for key in ("magMax", "source", "stars", "lines"):
        if key not in data:
            fail(name, f"no {key}")
    for i, star in enumerate(data.get("stars", [])[:200]):
        if len(star) != 3 or not all(isinstance(v, (int, float)) for v in star):
            fail(name, f"star {i} is not [ra, dec, mag]")
            break


SHAPES = {"topology": check_topology, "sky": check_sky}


# ---------- invariants ----------

def invariants(files):
    countries = files.get("atlas/countries-50m.json")
    detail = files.get("atlas/countries-10m.json")
    for name, topo in (("atlas/countries-50m.json", countries), ("atlas/countries-10m.json", detail)):
        if not topo:
            continue
        names = {g.get("properties", {}).get("name") for g in topo["objects"]["countries"]["geometries"]}
        for expected in ("Antarctica", "Japan", "Spain", "Vietnam"):
            if expected not in names:
                fail(name, f"{expected} is missing — the atlas or its property names changed")
        if len(names) < 200:
            fail(name, f"only {len(names)} countries")

    marine = files.get("atlas/marine-areas.json")
    if marine:
        for key in ("areas", "borders"):
            if key not in marine.get("objects", {}):
                fail("atlas/marine-areas.json", f"no {key} object")
        areas = len(marine["objects"].get("areas", {}).get("geometries", []))
        borders = len(marine["objects"].get("borders", {}).get("geometries", []))
        if areas < 200 or borders < 150:
            fail("atlas/marine-areas.json", f"{areas} named waters and {borders} boundaries"
                 " — Natural Earth ships more than that")
        # The sliver bug that drew an outline around the whole map is not tested here.
        # After quantization a sliver measures larger than some real straits, so any
        # threshold that catches it also fails Bab el Mandeb. It is guarded where it can
        # be measured honestly: build-atlas.py drops sub-square-kilometre rings from the
        # raw coordinates, and the consuming repo's render check catches what gets past.

    sky = files.get("sky/stars.json")
    if sky:
        mags = [s[2] for s in sky["stars"]]
        decs = [s[1] for s in sky["stars"]]
        if max(mags) > sky["magMax"] + 0.05:
            fail("sky/stars.json", f"a star at magnitude {max(mags)} is fainter than magMax")
        if max(decs) < 80 or min(decs) > -80:
            fail("sky/stars.json", "the catalogue does not reach both poles")
        if not 3000 < len(sky["stars"]) < 8000:
            fail("sky/stars.json", f"{len(sky['stars'])} stars is outside the expected range")


# ---------- drift ----------

def drift(files, previous):
    for name, data in files.items():
        was = (previous.get("datasets", {}).get(name) or {}).get("rows")
        now = count(name, data)
        if was and now < was * (1 - DRIFT):
            fail(name, f"{now} rows, was {was} — a drop of {(1 - now / was):.0%}; refusing to publish")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="do not rewrite MANIFEST.json")
    args = parser.parse_args()

    previous = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    files = {}
    for name, shape in DATASETS.items():
        path = ROOT / name
        if not path.exists():
            fail(name, "missing")
            continue
        try:
            files[name] = json.loads(path.read_text())
        except json.JSONDecodeError as error:
            fail(name, f"not valid JSON ({error})")
            continue
        SHAPES[shape](name, files[name])

    invariants(files)
    drift(files, previous)

    for name in DATASETS:
        path = ROOT / name
        mark = "  " if not any(p.startswith(name) for p in problems) else "!!"
        if path.exists():
            print(f"{mark} {name:34} {count(name, files.get(name, {})):>7} rows  "
                  f"{path.stat().st_size / 1024:>6.0f} KB")

    if problems:
        print("\n" + "\n".join("FAIL " + p for p in problems), file=sys.stderr)
        raise SystemExit(1)

    if not args.check:
        MANIFEST.write_text(json.dumps({
            "built": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "datasets": {
                name: {"rows": count(name, files[name]), "bytes": (ROOT / name).stat().st_size}
                for name in files
            },
        }, indent=2) + "\n")
        print(f"\n{MANIFEST.name} updated")


if __name__ == "__main__":
    main()
