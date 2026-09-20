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

# Everything here comes from the Action API, not the query service. WDQS throttles hard
# per IP and a block there lasts for many minutes; api.php is a separate, far more generous
# limiter, and wbgetentities hands back labels, descriptions, claims and sitelinks for 50
# entities in a single request — so a whole trip is two calls rather than five SPARQL
# queries. Nothing below needs to traverse the graph, so nothing below needs SPARQL.
API = "https://www.wikidata.org/w/api.php"
# Wikimedia's User-Agent policy asks for a contact; without one, requests look anonymous
# and are throttled sooner.
AGENT = ("zhang-en-yao.github.io places builder (build-places.py; "
         "+https://github.com/Zhang-En-Yao/assets.core)")
GAP_S = 1.0
BATCH_SIZE = 50  # wbgetentities' documented ceiling for ids per request.
# Waiting out one Retry-After should be enough for a short burst. If the very next request
# gets 429 again, this is a longer block that a fixed wait-and-retry won't fix — fail fast
# instead of silently grinding through 8 attempts (which could take hours).
MAX_RATE_LIMIT_RETRIES = 2

# Claims the fact card shows. Everything Wikidata has is written out; how many of them a
# card prints is the page's business, not this script's.
CLAIMS = [
    ("type", "P31"), ("style", "P149"), ("religion", "P140"),
    ("architect", "P84"), ("founder", "P112"), ("heritage", "P1435"),
]
DATES = [("inception", "P571"), ("opened", "P1619"), ("ended", "P576")]


def format_duration(seconds):
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def countdown_sleep(seconds, label):
    """Prints remaining time every 10s, so a long wait doesn't look like a hang."""
    remaining = seconds
    while remaining > 0:
        print(f"  {label}: {format_duration(remaining)} left", file=sys.stderr)
        step = min(10, remaining)
        time.sleep(step)
        remaining -= step


def get(url):
    request = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": "application/json"})
    rate_limit_hits = 0
    for attempt in range(8):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code != 429:
                raise
            wait = max(90, int(error.headers.get("Retry-After", 0)))
            rate_limit_hits += 1
            if rate_limit_hits > MAX_RATE_LIMIT_RETRIES:
                raise SystemExit(
                    f"still 429 after waiting {format_duration(wait)} and retrying "
                    f"{MAX_RATE_LIMIT_RETRIES} time(s) — Wikidata has this IP under a longer "
                    "block, not a short burst limit. Waiting the same amount again won't help; "
                    "try again later."
                )
            countdown_sleep(wait, "429 — locked out")
        except OSError as error:
            if attempt == 7:
                raise
            wait = 2 * (attempt + 1)
            print(f"  retrying in {format_duration(wait)} ({error})", file=sys.stderr)
            time.sleep(wait)


def entities(qids, props, **extra):
    """{QID: entity} from wbgetentities, in batches, for whichever props are asked for."""
    out = {}
    for i in range(0, len(qids), BATCH_SIZE):
        query = dict(action="wbgetentities", format="json", formatversion="2",
                     ids="|".join(qids[i:i + BATCH_SIZE]), props=props,
                     languages="en", **extra)
        time.sleep(GAP_S)
        payload = get(f"{API}?{urllib.parse.urlencode(query)}")
        # One unusable id fails the whole batch, so say which and what was wrong with it
        # rather than letting every entity in the batch look like it came back empty.
        if "error" in payload:
            error = payload["error"]
            raise SystemExit(f"Wikidata rejected a request for "
                             f"{', '.join(qids[i:i + BATCH_SIZE])}: "
                             f"{error.get('code')} — {error.get('info')}")
        out.update(payload.get("entities", {}))
    return out


def best(entity, prop):
    """The statements wdt: would have returned: preferred rank if any, never deprecated."""
    claims = [c for c in entity.get("claims", {}).get(prop, [])
              if c.get("rank") != "deprecated" and c.get("mainsnak", {}).get("snaktype") == "value"]
    preferred = [c for c in claims if c.get("rank") == "preferred"]
    return preferred or claims


def values(entity, prop):
    """The QIDs a wikibase-item claim points at, in order, deduplicated."""
    out = []
    for claim in best(entity, prop):
        value = claim["mainsnak"]["datavalue"]["value"]
        if isinstance(value, dict) and value.get("id") and value["id"] not in out:
            out.append(value["id"])
    return out


def year_of(stamp):
    """Wikidata's +1865-01-01T00:00:00Z as a plain year, or None for an unknown-value node."""
    if not re.match(r"^[+-]?\d{3,4}-\d{2}-\d{2}T", stamp):
        return None
    sign, digits = ("-", stamp[1:]) if stamp[0] == "-" else ("", stamp.lstrip("+"))
    return sign + str(int(digits.split("-")[0]))

def heritage_listing(qids, fetched):
    """Each item's World Heritage listing: its own, or the one it is (a part of a) part of.

    SPARQL would walk this with a property path; two more wbgetentities calls do the same
    two hops, and only for the items that actually claim to be part of something.
    """
    listings, known = {}, dict(fetched)
    frontier = {q: [q] for q in qids}
    for level in range(3):  # the item itself, then two levels of P361, as the old query did.
        for qid, nodes in frontier.items():
            for node in nodes:
                reference = best(known.get(node, {}), "P757")
                if reference:
                    listings[qid] = {"id": node,
                                     "ref": reference[0]["mainsnak"]["datavalue"]["value"]}
                    break
        if level == 2:
            break  # the next level's parents would be fetched and never looked at.
        climb = {}
        for qid, nodes in frontier.items():
            if qid in listings:
                continue
            # Every P361 parent, not just the first: an item can be part of a city *and*
            # of the heritage site, and the site is not always the first statement.
            parents = []
            for node in nodes:
                for parent in values(known.get(node, {}), "P361"):
                    if parent not in parents:
                        parents.append(parent)
            if parents:
                climb[qid] = parents
        frontier = climb
        if not frontier:
            break
        missing = sorted({p for ps in frontier.values() for p in ps} - set(known))
        if missing:
            known.update(entities(missing, "claims"))
    return listings


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
    # One request per 50 entities brings back everything below except the labels of the
    # QIDs the claims point at, which is one more request for all of them together.
    fetched = entities(qids, "labels|descriptions|claims|sitelinks", sitefilter="enwiki")
    # An id that no longer resolves is an editorial problem, not a fetching one: the item
    # was deleted or merged away under you. Name it and stop, rather than quietly writing
    # an empty record that only shows up later as a point with no coordinate.
    gone = [q for q in qids if q not in fetched or "missing" in fetched[q]]
    if gone:
        raise SystemExit(
            "Wikidata has no item for " + ", ".join(gone) + ".\n"
            "Deleted, or merged into another item. Look each one up and put the id it "
            "redirects to in content.json — which entity a place is stays your call.")

    places = {q: {} for q in qids}
    for qid, entity in fetched.items():
        place = places[qid]
        label = entity.get("labels", {}).get("en", {}).get("value")
        description = entity.get("descriptions", {}).get("en", {}).get("value")
        if label:
            place["label"] = label
        if description:
            place["description"] = description
        coord = best(entity, "P625")
        if coord:
            value = coord[0]["mainsnak"]["datavalue"]["value"]
            place["coord"] = [round(value["latitude"], 6), round(value["longitude"], 6)]
        title = entity.get("sitelinks", {}).get("enwiki", {}).get("title")
        if title:
            place["wikipedia"] = title

    # "part of a World Heritage Site" and "World Heritage Site" say nothing the
    # heritageSite link below does not say better.
    skip = {"Q43113623", "Q9259"}
    listings = heritage_listing(qids, fetched)
    for qid, listing in listings.items():
        places[qid]["heritageSite"] = listing

    sites = {}
    site_entities = entities(sorted({w["id"] for w in listings.values()}),
                             "labels|claims") if listings else {}
    for qid, entity in site_entities.items():
        site = sites.setdefault(qid, {"criteria": []})
        label = entity.get("labels", {}).get("en", {}).get("value")
        if label:
            site["label"] = label
        for claim in best(entity, "P1435"):
            if claim["mainsnak"]["datavalue"]["value"].get("id") != "Q9259":
                continue
            for qualifier in claim.get("qualifiers", {}).get("P580", []):
                if qualifier.get("snaktype") == "value":
                    year = year_of(qualifier["datavalue"]["value"]["time"])
                    if year:
                        site["inscribed"] = year

    # Everything the cards print by name — claim values and heritage criteria — arrives as
    # bare QIDs, so resolve the whole lot in one pass rather than per property.
    wanted = {qid: {key: [v for v in values(fetched[qid], prop) if v not in skip]
                    for key, prop in CLAIMS} for qid in qids}
    criteria = {qid: values(entity, "P2614") for qid, entity in site_entities.items()}
    needed = sorted({v for bykey in wanted.values() for vs in bykey.values() for v in vs}
                    | {v for vs in criteria.values() for v in vs})
    labels = {qid: e.get("labels", {}).get("en", {}).get("value")
              for qid, e in entities(needed, "labels").items()} if needed else {}

    for key, _ in CLAIMS:
        for qid in qids:
            found = [{"id": v, "label": labels[v]} for v in wanted[qid][key] if labels.get(v)]
            if found:
                places[qid][key] = found
        print(f"  {key}: {sum(1 for p in places.values() if key in p)}")
    for key, prop in DATES:
        for qid in qids:
            years = []
            for claim in best(fetched[qid], prop):
                year = year_of(claim["mainsnak"]["datavalue"]["value"]["time"])
                if year and year not in years:
                    years.append(year)
            if years:
                places[qid][key] = years
    for qid, site in sites.items():
        site["criteria"] = [labels[v] for v in criteria.get(qid, []) if labels.get(v)]

    OUT.write_text(json.dumps({"places": places, "heritageSites": sites},
                              ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"{OUT.name}: {len(places)} entities, {len(sites)} World Heritage sites, "
          f"{OUT.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
