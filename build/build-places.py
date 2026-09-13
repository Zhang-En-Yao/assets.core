#!/usr/bin/env python3
"""Regenerate a trip's places.json — the Wikidata record behind each of its points.

Run from a trip repository: content.json in, places.json out.

This script does not guess. Each point's `wikidata` key is the join, and all this does is
follow it: no name matching, no proximity scoring, no writing back to content.json. Which
entity a place *is* is an editorial decision, and it is recorded where you can see and
change it, in the file you edit. Three states, all of them explicit:

    "wikidata": "Q705949"   linked — this is the entity
    "wikidata": null        checked — no Wikidata item exists for this place
    key absent              not looked at yet

Everything written is in Wikidata's own English labels; nothing is translated, reworded
or interpreted. Coordinates are never written back either: content.json keeps the ones
you typed, places.json carries Wikidata's, and validate-trip.py reports where the two
disagree so you can decide.

    python3 ../assets.core/build/build-places.py
"""

import argparse
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = pathlib.Path.cwd()
CONTENT = REPO / "content.json"
OUT = REPO / "places.json"

SPARQL = "https://query.wikidata.org/sparql"
AGENT = "zhang-en-yao.github.io places builder (build-places.py)"
GAP_S = 2.0
BATCH_SIZE = 30

# Claims the fact card shows. Everything Wikidata has is written out; how many of them a
# card prints is the page's business, not this script's.
CLAIMS = [
    ("type", "P31"), ("style", "P149"), ("religion", "P140"),
    ("architect", "P84"), ("founder", "P112"), ("heritage", "P1435"),
]
DATES = [("inception", "P571"), ("opened", "P1619"), ("ended", "P576")]


def get(url):
    request = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": "application/json"})
    for attempt in range(8):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code != 429 or attempt == 7:
                raise
            wait = max(90, int(error.headers.get("Retry-After", 0)))
            print(f"  rate-limited, waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
        except OSError as error:
            if attempt == 7:
                raise
            print(f"  retrying ({error})", file=sys.stderr)
            time.sleep(2 * (attempt + 1))


def sparql(query):
    time.sleep(GAP_S)
    return get(f"{SPARQL}?format=json&query={urllib.parse.quote(query)}")["results"]["bindings"]


def pull(qids, props):
    """{key: {item QID: [{id, label}]}} for a set of claims, in Wikidata's own English labels."""
    out = {key: {} for key, _ in props}
    for i in range(0, len(qids), BATCH_SIZE):
        values = " ".join("wd:" + q for q in qids[i:i + BATCH_SIZE])
        clauses = "\nUNION\n".join(
            f"""{{ ?item wdt:{prop} ?v . BIND({json.dumps(key)} AS ?kind) }}"""
            for key, prop in props
        )
        rows = sparql(f"""SELECT ?item ?v ?kind ?en WHERE {{ VALUES ?item {{ {values} }}
          {clauses}
          OPTIONAL {{ ?v rdfs:label ?en FILTER(LANG(?en) = "en") }} }}""")
        for row in rows:
            item = row["item"]["value"].rsplit("/", 1)[-1]
            value = row["v"]["value"].rsplit("/", 1)[-1]
            label = row.get("en", {}).get("value")
            seen = out[row["kind"]["value"]].setdefault(item, {})
            if label and value not in seen:
                seen[value] = label
    return {key: {q: [{"id": k, "label": v} for k, v in labels.items()] for q, labels in items.items()}
            for key, items in out.items()}


def pull_dates(qids, props):
    """{key: {item QID: [year, ...]}} for a set of date claims."""
    out = {key: {} for key, _ in props}
    for i in range(0, len(qids), BATCH_SIZE):
        values = " ".join("wd:" + q for q in qids[i:i + BATCH_SIZE])
        clauses = "\nUNION\n".join(
            f"""{{ ?item wdt:{prop} ?v . BIND({json.dumps(key)} AS ?kind) }}"""
            for key, prop in props
        )
        rows = sparql(f"""SELECT ?item ?v ?kind WHERE {{ VALUES ?item {{ {values} }}
          {clauses} }}""")
        for row in rows:
            stamp = row["v"]["value"]
            if not re.match(r"^-?\d{3,4}-\d{2}-\d{2}T", stamp):
                continue  # "unknown value" nodes come back as a hash
            sign, digits = ("-", stamp[1:]) if stamp[0] == "-" else ("", stamp)
            year = sign + str(int(digits.split("-")[0]))
            item = row["item"]["value"].rsplit("/", 1)[-1]
            years = out[row["kind"]["value"]].setdefault(item, [])
            if year not in years:
                years.append(year)
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
    argparse.ArgumentParser(description=__doc__).parse_args()
    if not CONTENT.exists():
        raise SystemExit(f"no {CONTENT.name} here — run this from a trip repository")
    content = json.loads(CONTENT.read_text())
    points = load_points(content)
    unchecked = [(p, w) for p, w in points if "wikidata" not in p]
    none = [(p, w) for p, w in points if p.get("wikidata", "") is None]
    print(f"{len(points)} points: {len(points) - len(unchecked) - len(none)} linked, "
          f"{len(none)} with no Wikidata item, {len(unchecked)} not looked at yet")
    for point, where in unchecked:
        print(f"  no wikidata key: {where} / {point['name']}")

    qids = sorted({p["wikidata"] for p, _ in points if p.get("wikidata")})
    if not qids:
        raise SystemExit("no linked points — nothing to fetch")
    print(f"{len(qids)} entities")

    places = {q: {} for q in qids}
    rows = []
    for i in range(0, len(qids), BATCH_SIZE):
        values = " ".join("wd:" + q for q in qids[i:i + BATCH_SIZE])
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
    pulled = pull(qids, CLAIMS)
    for key, _ in CLAIMS:
        for qid, values in pulled[key].items():
            values = [v for v in values if v["id"] not in skip]
            if values:
                places[qid][key] = values
        print(f"  {key}: {sum(1 for p in places.values() if key in p)}")
    pulled_dates = pull_dates(qids, DATES)
    for key, _ in DATES:
        for qid, years in pulled_dates[key].items():
            places[qid][key] = years

    for lang in ("en",):
        for i in range(0, len(qids), BATCH_SIZE):
            values = " ".join("wd:" + q for q in qids[i:i + BATCH_SIZE])
            for row in sparql(f"""SELECT ?item ?site WHERE {{ VALUES ?item {{ {values} }}
              ?site schema:about ?item ; schema:isPartOf <https://{lang}.wikipedia.org/> . }}"""):
                title = row["site"]["value"].split("/wiki/", 1)[-1]
                places[row["item"]["value"].rsplit("/", 1)[-1]]["wikipedia"] = \
                    urllib.parse.unquote(title).replace("_", " ")

    # World Heritage: the item's own listing, or the one it is a part of.
    listings, sites = {}, {}
    for i in range(0, len(qids), BATCH_SIZE):
        values = " ".join("wd:" + q for q in qids[i:i + BATCH_SIZE])
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


if __name__ == "__main__":
    main()
