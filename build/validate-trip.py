#!/usr/bin/env python3
"""Check a trip repository before it is published, and refresh its MANIFEST.json.

Run from the trip repository. The three files there have to agree with each other, and
that agreement is what this checks — a schema can say places.json is well formed, but
only content.json can say whether it holds the right places.

  content.json   every point has a name and a coordinate
  places.json    every `wikidata` key in content.json has a record here, and every
                 heritageSite a record points at is present
  streets.json   its keys are exactly the map keys build-streets.py derives from
                 content.json — rename a section and its maps are orphaned, silently,
                 until someone opens the page
  drift          nothing lost more than a twentieth of its rows since the last build

Exit code 1 on any failure, so a workflow stops before committing.

    python3 ../assets.core/build/validate-trip.py
    python3 ../assets.core/build/validate-trip.py --check   # leave MANIFEST.json alone
"""

import argparse
import datetime
import importlib.util
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = pathlib.Path.cwd()
MANIFEST = REPO / "MANIFEST.json"

DRIFT = 0.05

problems = []


def fail(where, message):
    problems.append(f"{where}: {message}")


def load(name, required=True):
    path = REPO / name
    if not path.exists():
        if required:
            fail(name, "missing")
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as error:
        fail(name, f"not valid JSON ({error})")
        return None


def points_of(content):
    for section in content.get("sections", []):
        for sub in section.get("subsections", []):
            for point in sub.get("points", []):
                yield point, f'{section.get("heading")} / {sub.get("heading")}'
        for point in section.get("points", []):
            yield point, section.get("heading")


def street_keys(content):
    """The keys build-streets.py would write, imported from the script itself so the two
    can never drift apart."""
    spec = importlib.util.spec_from_file_location("streets", HERE / "build-streets.py")
    streets = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(streets)
    return {key for key, _, _ in streets.maps_for_json_trip(content)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="do not rewrite MANIFEST.json")
    args = parser.parse_args()

    content = load("content.json")
    places = load("places.json", required=False)
    streets = load("streets.json", required=False)
    if content is None:
        print("FAIL content.json: missing", file=sys.stderr)
        raise SystemExit(1)

    if not content.get("title"):
        fail("content.json", "no title")
    if not content.get("sections"):
        fail("content.json", "no sections")

    points = list(points_of(content))
    if not points:
        fail("content.json", "no points")
    for point, where in points:
        if not point.get("name"):
            fail("content.json", f"a point in {where} has no name")
        for key in ("lat", "lon"):
            if not isinstance(point.get(key), (int, float)):
                fail("content.json", f'{where} / {point.get("name")} has no {key}')

    linked = [p["wikidata"] for p, _ in points if p.get("wikidata")]
    if places is not None:
        for point, where in points:
            qid = point.get("wikidata")
            if qid and qid not in places.get("places", {}):
                fail("places.json", f'{where} / {point["name"]} points at {qid}, which is not here')
        for qid, place in places.get("places", {}).items():
            site = (place.get("heritageSite") or {}).get("id")
            if site and site not in places.get("heritageSites", {}):
                fail("places.json", f"{qid} cites World Heritage site {site}, which is not here")
        extra = set(places.get("places", {})) - set(linked)
        if extra:
            fail("places.json", f"{len(extra)} records no point links to: {sorted(extra)[:4]}")
    elif linked:
        fail("places.json", f"missing, but {len(linked)} points are linked to Wikidata")

    if streets is not None:
        expected = street_keys(content)
        orphaned = set(streets) - expected
        if orphaned:
            fail("streets.json", f"{len(orphaned)} maps nothing asks for — a heading was renamed:"
                                 f" {sorted(orphaned)[:3]}")
        # A missing map is a gap in the page, not a broken build: Overpass fails often
        # enough that a partial fetch is normal. Say so, do not fail.
        for key in sorted(expected - set(streets)):
            print(f"   no streets yet for {key}")
        empty = [k for k, v in streets.items() if not v]
        if empty:
            print(f"   {len(empty)} maps fetched but empty: {empty[:3]}")

    rows = {
        "content.json": len(points),
        "places.json": len(places.get("places", {})) if places else 0,
        "streets.json": sum(len(v) for v in streets.values()) if streets else 0,
    }
    previous = (json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}).get("datasets", {})
    for name, now in rows.items():
        was = (previous.get(name) or {}).get("rows")
        if was and now < was * (1 - DRIFT):
            fail(name, f"{now} rows, was {was} — a drop of {(1 - now / was):.0%}; refusing to publish")

    for name, count in rows.items():
        path = REPO / name
        mark = "!!" if any(p.startswith(name) for p in problems) else "  "
        size = f"{path.stat().st_size / 1024:6.0f} KB" if path.exists() else "  missing"
        print(f"{mark} {name:16} {count:>6} rows  {size}")

    if problems:
        print("\n" + "\n".join("FAIL " + p for p in problems), file=sys.stderr)
        raise SystemExit(1)

    if not args.check:
        MANIFEST.write_text(json.dumps({
            "trip": REPO.name.replace("assets.trip.", ""),
            "title": content.get("title"),
            "built": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "datasets": {
                name: {"rows": rows[name], "bytes": (REPO / name).stat().st_size}
                for name in rows if (REPO / name).exists()
            },
        }, indent=2) + "\n")
        print(f"\n{MANIFEST.name} updated")


if __name__ == "__main__":
    main()
