#!/usr/bin/env python3
"""Regenerate assets/data/streets/<trip-id>.json: OpenStreetMap street geometry for a trip's
cluster maps, fetched once from Overpass so pages never query it live.

Map framing and grouping must match assets/js/trip/maps.js (AREA) and
assets/js/trip/clusters.js (CLUSTER_CAP_M, SAME_SITE_M); keys match assets/js/trip/content.js.

Run from a trip repository, which supplies content.json and receives streets.json. Maps
already in streets.json are skipped, so a re-run only fetches what is new.

    python3 ../assets.core/build/build-streets.py
    python3 ../assets.core/build/build-streets.py --refetch "Kyoto / Fushimi"
    python3 ../assets.core/build/build-streets.py --all
"""

import argparse
import email.utils
import json
import math
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# Run from a trip repository: content.json in, streets.json out, both in the working
# directory. The script itself lives in assets.core so every trip shares one copy.
REPO = pathlib.Path.cwd()
CONTENT = REPO / "content.json"
OUT = REPO / "streets.json"

# Tried in order; the public instance sometimes firewalls bursts of requests.
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
REQUEST_GAP_S = 4  # Politeness gap between requests to the shared public instance.

# Mirror assets/js/trip/maps.js (AREA) and assets/js/trip/clusters.js.
AREA_SIZE = 700
AREA_MIN_SPAN_LON = 0.004
CLUSTER_CAP_M = 3500
SAME_SITE_M = 300

# Which OSM highway types to fetch, and the query box margin, per kind of map.
PROFILES = {
    # A neighbourhood: the full street grid, minus steps/tracks/service roads, which are
    # mostly noise at this scale. Footways/paths stay so plazas and promenades aren't blank.
    "city": {
        "margin": 1.6,
        "highway_types": (
            "motorway|trunk|primary|secondary|tertiary|unclassified|residential|"
            "living_street|pedestrian|footway|path|cycleway|motorway_link|trunk_link|"
            "primary_link|secondary_link|tertiary_link"
        ),
    },
    # A ~115 km route: main roads plus `path` (which carries the trail itself). Anything finer
    # times out the public instance. The smaller margin shrinks only the query box, not the map.
    "route": {
        "margin": 1.15,
        "highway_types": "primary|secondary|tertiary|path|primary_link|secondary_link|tertiary_link",
    },
}


def cluster_bounds(points, margin, min_span_lon, w, h):
    """Mirrors framingBox() in assets/js/trip/maps.js."""
    lons = [p["lon"] for p in points]
    lats = [p["lat"] for p in points]
    west, east = min(lons), max(lons)
    south, north = min(lats), max(lats)
    span_lon = max((east - west) * margin, min_span_lon)
    span_lat = max((north - south) * margin, min_span_lon * h / w)
    cx, cy = (west + east) / 2, (south + north) / 2
    return {
        "west": cx - span_lon / 2,
        "east": cx + span_lon / 2,
        "south": cy - span_lat / 2,
        "north": cy + span_lat / 2,
    }


def haversine_m(a, b):
    R = 6371000
    lat1, lon1, lat2, lon2 = map(math.radians, [a["lat"], a["lon"], b["lat"], b["lon"]])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def cluster_points(points, cap_m):
    """Mirrors clusterPoints() in assets/js/trip/clusters.js (single-linkage union-find)."""
    n = len(points)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            if haversine_m(points[i], points[j]) <= cap_m:
                union(i, j)

    order, groups = [], {}
    for i in range(n):
        r = find(i)
        if r not in groups:
            groups[r] = []
            order.append(r)
        groups[r].append(points[i])
    return [groups[r] for r in order]


def format_duration(seconds):
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def parse_retry_after(value):
    """Retry-After is either a number of seconds or an HTTP-date (RFC 7231)."""
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, (dt - datetime.now(timezone.utc)).total_seconds())


def fetch_streets(bounds, highway_types):
    query = (
        "[out:json][timeout:25];"
        f'way["highway"~"^({highway_types})$"]({bounds["south"]},{bounds["west"]},{bounds["north"]},{bounds["east"]});'
        "out geom;"
    )
    data = urllib.parse.urlencode({"data": query}).encode()
    # Overpass rejects Python's default User-Agent with a 406.
    headers = {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "curl/8.4.0"}

    # Two tries per instance (504s and 429s are usually transient), then the next instance.
    attempts = [(url, attempt) for url in OVERPASS_URLS for attempt in range(2)]
    last_err = None
    for i, (url, attempt) in enumerate(attempts):
        try:
            req = urllib.request.Request(url, data=data, method="POST", headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                result = json.load(r)
            ways = [e for e in result.get("elements", []) if e.get("type") == "way" and e.get("geometry")]
            # Coordinates only, rounded to ~1 m.
            return [
                [[round(n["lon"], 5), round(n["lat"], 5)] for n in w["geometry"]]
                for w in ways
            ]
        except urllib.error.HTTPError as e:
            last_err = e
            wait = REQUEST_GAP_S * (attempt + 2)
            if e.code == 429:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                locked_for = parse_retry_after(retry_after) if retry_after else None
                if locked_for is not None:
                    wait = locked_for
                    print(f"  429 — locked out for {format_duration(wait)} (Retry-After: {retry_after})")
                else:
                    print(f"  429 — rate-limited, no Retry-After given; backing off {format_duration(wait)}")
            else:
                print(f"  HTTP {e.code}")
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            # OSError covers dropped connections, which aren't URLErrors.
            last_err = e
            wait = REQUEST_GAP_S * (attempt + 2)
        if i < len(attempts) - 1:
            print(f"  retrying in {format_duration(wait)}...")
            time.sleep(wait)
    raise last_err


def fetch_for_profile(points, profile_name):
    profile = PROFILES[profile_name]
    bounds = cluster_bounds(points, profile["margin"], AREA_MIN_SPAN_LON, AREA_SIZE, AREA_SIZE)
    return fetch_streets(bounds, profile["highway_types"])


def maps_for_json_trip(content):
    """Yields (key, points, profile_name) for every map assets/js/trip/content.js draws."""
    for section in content.get("sections", []):
        subs = section.get("subsections", [])
        if section.get("route"):
            pooled = [p for s in subs for p in s.get("points", [])]
            points = [group[0] for group in cluster_points(pooled, SAME_SITE_M)]
            if points:
                yield section["heading"], points, "route"
            continue
        for sub in subs:
            points = sub.get("points", [])
            if not points:
                continue
            base = f'{section["heading"]} / {sub["heading"]}'
            groups = cluster_points(points, CLUSTER_CAP_M)
            for i, group in enumerate(groups):
                key = base if len(groups) == 1 else f"{base} #{i + 1}"
                yield key, group, "city"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--refetch", metavar="KEY", nargs="+",
        help="Drop cached maps whose key equals this, or starts with this followed by ' #' or "
             "' / ' — so naming a whole section's heading re-fetches every map under it, and "
             "naming one 'Section / Subsection' re-fetches just that one (every part of it, "
             "if it was itself split). Re-fetches instead of skipping as already-done.",
    )
    group.add_argument(
        "--all", action="store_true",
        help="Drop every cached map first (scoped by --trip if given) — a full re-fetch, "
             "e.g. after changing a PROFILES entry.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if not CONTENT.exists():
        raise SystemExit(f"no {CONTENT.name} here — run this from a trip repository")
    maps = list(maps_for_json_trip(json.loads(CONTENT.read_text())))
    if not maps:
        raise SystemExit("content.json has no points to draw maps around")

    # Resumable: cached maps are kept unless --refetch/--all drops them. Overpass is a
    # shared public instance and a full run is a few dozen queries, so never re-ask for
    # something already on disk.
    out = json.loads(OUT.read_text()) if OUT.exists() else {}
    if args.all:
        out = {}
    elif args.refetch:
        for name in args.refetch:
            out.pop(name, None)
            for key in [k for k in out if k.startswith(f"{name} #") or k.startswith(f"{name} / ")]:
                del out[key]
    pending = [(key, points, profile) for key, points, profile in maps if key not in out]
    print(f"{len(maps)} maps, {len(pending)} to fetch")

    failures = 0
    durations = []
    for i, (key, points, profile_name) in enumerate(pending):
        eta = ""
        if durations:
            avg = sum(durations) / len(durations)
            eta = f", ~{format_duration(avg * (len(pending) - i))} left"
        print(f"[{i + 1}/{len(pending)}] {key} ({profile_name}): querying Overpass…{eta}")
        t0 = time.monotonic()
        try:
            out[key] = fetch_for_profile(points, profile_name)
            print(f"  {len(out[key])} ways")
            OUT.write_text(json.dumps(out, ensure_ascii=False) + "\n")
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            failures += 1
            print(f"  FAILED: {e} — leaving this cluster without streets")
        durations.append(time.monotonic() - t0)
        if i < len(pending) - 1:
            time.sleep(REQUEST_GAP_S)

    if out:
        print(f"{OUT.name}: {len(out)} maps, {sum(len(v) for v in out.values())} ways total")
    if durations:
        print(f"took {format_duration(sum(durations) + REQUEST_GAP_S * max(len(durations) - 1, 0))}")
    # A partial fetch is worth keeping, but the workflow should not publish it as if it
    # were complete.
    if failures:
        raise SystemExit(f"{failures} of {len(pending)} maps failed")


if __name__ == "__main__":
    main()
