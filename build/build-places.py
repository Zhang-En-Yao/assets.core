#!/usr/bin/env python3
"""Regenerate a trip's places.json — the Wikidata record behind each of its points.

Run from a trip repository: content.json in, places.json out. Each point's `wikidata` key
is the join, and this script only follows it. Everything written is in Wikidata's own
English labels; nothing is translated, reworded or interpreted.

A point with no `wikidata` key is matched by name crossed with proximity to the
coordinate on file, and the result is printed for you to check — but only `--link` writes
it back into content.json, because which entity a place *is* is an editorial decision and
belongs in the file you edit, not in a table inside a build script. A point that genuinely
has no Wikidata item (a cafe, an ice cream counter) is given `"wikidata": null`, which
stops the matcher asking about it again.

Coordinates are never written back. content.json keeps the ones you typed, places.json
carries Wikidata's, and the page decides which to draw.

    python3 ../assets.core/build/build-places.py           # fetch for the linked points
    python3 ../assets.core/build/build-places.py --link    # also resolve and save new QIDs
"""

import argparse
import json
import math
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request

REPO = pathlib.Path.cwd()
CONTENT = REPO / "content.json"
OUT = REPO / "places.json"

API = "https://www.wikidata.org/w/api.php"
SPARQL = "https://query.wikidata.org/sparql"
AGENT = "zhang-en-yao.github.io places builder (build-places.py)"
GAP_S = 0.15

MAG_NAME_MIN = 0.55  # name similarity a search hit needs before it is accepted
MAX_M = 1200         # …and how far it may sit from the coordinate already on file
# Claims the fact card shows. Everything Wikidata has is written out; how many of them a
# card prints is the page's business, not this script's.
CLAIMS = [
    ("type", "P31"), ("style", "P149"), ("religion", "P140"),
    ("architect", "P84"), ("founder", "P112"), ("heritage", "P1435"),
]
DATES = [("inception", "P571"), ("opened", "P1619"), ("ended", "P576")]


def get(url):
    request = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except OSError as error:
            if attempt == 3:
                raise
            print(f"  retrying ({error})", file=sys.stderr)
            time.sleep(2 * (attempt + 1))


def sparql(query):
    time.sleep(GAP_S)
    return get(f"{SPARQL}?format=json&query={urllib.parse.quote(query)}")["results"]["bindings"]


def search(name):
    time.sleep(GAP_S)
    url = (f"{API}?action=wbsearchentities&format=json&language=en&uselang=en&type=item"
           f"&limit=12&search={urllib.parse.quote(name)}")
    return [(h["id"], h.get("label", ""), h.get("aliases") or []) for h in get(url).get("search", [])]


def haversine(lat1, lon1, lat2, lon2):
    r, d = 6371000.0, math.pi / 180
    a = (math.sin((lat2 - lat1) * d / 2) ** 2
         + math.cos(lat1 * d) * math.cos(lat2 * d) * math.sin((lon2 - lon1) * d / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def similarity(a, b):
    norm = lambda s: re.sub(r"[^a-z0-9　-鿿]+", " ", s.lower()).strip()
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    x, y = set(a.split()), set(b.split())
    jaccard = len(x & y) / len(x | y)
    return max(jaccard, 0.75 if a in b or b in a else 0.0)


def variants(name):
    out = [name]
    paren = re.match(r"^(.*?)\s*\((.*?)\)\s*$", name)
    if paren:
        out += [paren.group(1), paren.group(2)]
    out += re.split(r"\s*[/,]\s*", name)
    return [v.strip() for v in dict.fromkeys(out) if len(v.strip()) > 2]


def coords_of(qids):
    out = {}
    for i in range(0, len(qids), 200):
        values = " ".join("wd:" + q for q in qids[i:i + 200])
        rows = sparql(f"SELECT ?item ?c WHERE {{ VALUES ?item {{ {values} }} ?item wdt:P625 ?c . }}")
        for row in rows:
            m = re.search(r"Point\(([-\d.]+) ([-\d.]+)\)", row["c"]["value"])
            if m:
                out[row["item"]["value"].rsplit("/", 1)[-1]] = (float(m.group(2)), float(m.group(1)))
    return out


def resolve(point):
    """The best Wikidata item for a point, or None."""
    hits = []
    for name in variants(point["name"]):
        hits += search(name)
        if hits and name == point["name"]:
            break
    hits = list({h[0]: h for h in hits}.values())
    if not hits:
        return None
    coords = coords_of([h[0] for h in hits])
    best, best_score = None, 0.0
    for qid, label, aliases in hits:
        xy = coords.get(qid)
        if not xy:
            continue
        distance = haversine(point["lat"], point["lon"], *xy)
        name_score = max([similarity(v, label) for v in variants(point["name"])]
                         + [similarity(point["name"], a) for a in aliases])
        if name_score < MAG_NAME_MIN or distance > MAX_M:
            continue
        score = name_score * 0.7 + math.exp(-distance / 250) * 0.3
        if score > best_score:
            best, best_score = qid, score
    return best


def pull(qids, prop):
    """{item QID: [{id, label}]} for one claim, in Wikidata's own English labels."""
    out = {}
    for i in range(0, len(qids), 120):
        values = " ".join("wd:" + q for q in qids[i:i + 120])
        rows = sparql(f"""SELECT ?item ?v ?en WHERE {{ VALUES ?item {{ {values} }}
          ?item wdt:{prop} ?v .
          OPTIONAL {{ ?v rdfs:label ?en FILTER(LANG(?en) = "en") }} }}""")
        for row in rows:
            item = row["item"]["value"].rsplit("/", 1)[-1]
            value = row["v"]["value"].rsplit("/", 1)[-1]
            label = row.get("en", {}).get("value")
            seen = out.setdefault(item, {})
            if label and value not in seen:
                seen[value] = label
    return {q: [{"id": k, "label": v} for k, v in labels.items()] for q, labels in out.items()}


def pull_dates(qids, prop):
    out = {}
    for i in range(0, len(qids), 200):
        values = " ".join("wd:" + q for q in qids[i:i + 200])
        rows = sparql(f"SELECT ?item ?v WHERE {{ VALUES ?item {{ {values} }} ?item wdt:{prop} ?v . }}")
        for row in rows:
            stamp = row["v"]["value"]
            if not re.match(r"^-?\d{3,4}-\d{2}-\d{2}T", stamp):
                continue  # "unknown value" nodes come back as a hash
            sign, digits = ("-", stamp[1:]) if stamp[0] == "-" else ("", stamp)
            year = sign + str(int(digits.split("-")[0]))
            out.setdefault(row["item"]["value"].rsplit("/", 1)[-1], [])
            if year not in out[row["item"]["value"].rsplit("/", 1)[-1]]:
                out[row["item"]["value"].rsplit("/", 1)[-1]].append(year)
    return out


def load_points(content):
    """Every point in the travelogue, with the section it sits in, in document order."""
    points = []
    for section in content.get("sections", []):
        for sub in section.get("subsections", []):
            for point in sub.get("points", []):
                points.append((point, sub.get("heading")))
        for point in section.get("points", []):
            points.append((point, section.get("heading")))
    return points


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--link", action="store_true",
                        help="resolve points that have no wikidata key, and save what is found")
    args = parser.parse_args()

    if not CONTENT.exists():
        raise SystemExit(f"no {CONTENT.name} here — run this from a trip repository")
    raw = CONTENT.read_text()
    content = json.loads(raw)
    points = load_points(content)
    unlinked = [(p, w) for p, w in points if "wikidata" not in p]
    print(f"{len(points)} points, {len(points) - len(unlinked)} linked, {len(unlinked)} unlinked")

    if unlinked and args.link:
        for point, where in unlinked:
            point["wikidata"] = resolve(point)
            print(f"  {where} / {point['name']}: {point['wikidata'] or 'no match — recorded as null'}")
        CONTENT.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n")
        print(f"{CONTENT.name} updated — check the matches before committing")
    elif unlinked:
        for point, where in unlinked:
            print(f"  unlinked: {where} / {point['name']}  (run with --link to resolve)")

    qids = sorted({p["wikidata"] for p, _ in points if p.get("wikidata")})
    if not qids:
        raise SystemExit("no linked points — nothing to fetch")
    print(f"{len(qids)} entities")

    places = {q: {} for q in qids}
    rows = []
    for i in range(0, len(qids), 120):
        values = " ".join("wd:" + q for q in qids[i:i + 120])
        rows += sparql(f"""SELECT ?item ?c ?label ?description WHERE {{ VALUES ?item {{ {values} }}
          OPTIONAL {{ ?item wdt:P625 ?c }}
          OPTIONAL {{ ?item rdfs:label ?label FILTER(LANG(?label) = "en") }}
          OPTIONAL {{ ?item schema:description ?description FILTER(LANG(?description) = "en") }} }}""")
    for row in rows:
        place = places[row["item"]["value"].rsplit("/", 1)[-1]]
        if "c" in row and "coord" not in place:
            m = re.search(r"Point\(([-\d.]+) ([-\d.]+)\)", row["c"]["value"])
            if m:
                place["coord"] = [round(float(m.group(2)), 6), round(float(m.group(1)), 6)]
        for key in ("label", "description"):
            if key in row and key not in place:
                place[key] = row[key]["value"]

    # "part of a World Heritage Site" and "World Heritage Site" say nothing the
    # heritageSite link below does not say better.
    skip = {"Q43113623", "Q9259"}
    for key, prop in CLAIMS:
        for qid, values in pull(qids, prop).items():
            values = [v for v in values if v["id"] not in skip]
            if values:
                places[qid][key] = values
        print(f"  {key}: {sum(1 for p in places.values() if key in p)}")
    for key, prop in DATES:
        for qid, years in pull_dates(qids, prop).items():
            places[qid][key] = years

    for lang in ("en",):
        values = " ".join("wd:" + q for q in qids)
        for row in sparql(f"""SELECT ?item ?site WHERE {{ VALUES ?item {{ {values} }}
          ?site schema:about ?item ; schema:isPartOf <https://{lang}.wikipedia.org/> . }}"""):
            title = row["site"]["value"].split("/wiki/", 1)[-1]
            places[row["item"]["value"].rsplit("/", 1)[-1]]["wikipedia"] = \
                urllib.parse.unquote(title).replace("_", " ")

    # World Heritage: the item's own listing, or the one it is a part of.
    listings, sites = {}, {}
    for i in range(0, len(qids), 150):
        values = " ".join("wd:" + q for q in qids[i:i + 150])
        for row in sparql(f"""SELECT ?item ?whs ?id WHERE {{ VALUES ?item {{ {values} }}
          {{ ?item wdt:P757 ?id . BIND(?item AS ?whs) }} UNION {{ ?item wdt:P361 ?whs . ?whs wdt:P757 ?id }}
          UNION {{ ?item wdt:P361/wdt:P361 ?whs . ?whs wdt:P757 ?id }} }}"""):
            qid = row["item"]["value"].rsplit("/", 1)[-1]
            listings[qid] = {"id": row["whs"]["value"].rsplit("/", 1)[-1], "ref": row["id"]["value"]}
    for qid, listing in listings.items():
        places[qid]["heritageSite"] = listing
    if listings:
        values = " ".join("wd:" + w["id"] for w in listings.values())
        for row in sparql(f"""SELECT ?whs ?label ?crit ?critLabel ?year WHERE {{ VALUES ?whs {{ {values} }}
          OPTIONAL {{ ?whs rdfs:label ?label FILTER(LANG(?label) = "en") }}
          OPTIONAL {{ ?whs wdt:P2614 ?crit }}
          OPTIONAL {{ ?whs p:P1435 [ ps:P1435 wd:Q9259 ; pq:P580 ?year ] }}
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" }} }}"""):
            site = sites.setdefault(row["whs"]["value"].rsplit("/", 1)[-1], {"criteria": []})
            if "label" in row and "label" not in site:
                site["label"] = row["label"]["value"]
            if "year" in row:
                site["inscribed"] = row["year"]["value"][:4]
            if "critLabel" in row and row["critLabel"]["value"] not in site["criteria"]:
                site["criteria"].append(row["critLabel"]["value"])

    OUT.write_text(json.dumps({"places": places, "heritageSites": sites},
                              ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"{OUT.name}: {len(places)} entities, {len(sites)} World Heritage sites, "
          f"{OUT.stat().st_size / 1024:.0f} KB")

    # How far Wikidata's coordinate sits from the one in content.json. Nothing is moved —
    # this is the list to read before trusting a point on the map.
    drift = []
    for point, where in points:
        place = places.get(point.get("wikidata"))
        if place and "coord" in place and "lat" in point:
            metres = haversine(point["lat"], point["lon"], *place["coord"])
            if metres > 150:
                drift.append((round(metres), where, point["name"]))
    if drift:
        print(f"\n{len(drift)} points more than 150 m from Wikidata's coordinate:")
        for metres, where, name in sorted(drift, reverse=True):
            print(f"  {metres:6d} m  {where} / {name}")


if __name__ == "__main__":
    main()
