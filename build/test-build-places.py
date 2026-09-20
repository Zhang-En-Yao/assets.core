#!/usr/bin/env python3
"""Offline tests for the parts of build-places.py that read Wikidata's own JSON.

These exist because the script stopped asking the query service to interpret Wikidata and
started interpreting it itself. SPARQL's `wdt:` prefix quietly meant "preferred rank if any
statement has it, otherwise normal, never deprecated, never an unknown value" — a semantic
that came free and now lives in best(). Property paths meant "any parent, to this depth",
and that now lives in heritage_listing(). Both are easy to get subtly wrong in a way that
produces a plausible-looking places.json, so both are pinned here.

No network: every fixture is a hand-built fragment of the JSON wbgetentities returns.

    python3 build/test-build-places.py
"""

import importlib.util
import pathlib
import sys

spec = importlib.util.spec_from_file_location(
    "build_places", pathlib.Path(__file__).with_name("build-places.py"))
bp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bp)


def entity(**claims):
    """An entity whose claims are {property: [statement, ...]}."""
    return {"claims": dict(claims)}


def item(qid, rank="normal"):
    return {"rank": rank,
            "mainsnak": {"snaktype": "value", "datavalue": {"value": {"id": qid}}}}


def text(value, rank="normal"):
    return {"rank": rank, "mainsnak": {"snaktype": "value", "datavalue": {"value": value}}}


def when(stamp, rank="normal"):
    return {"rank": rank,
            "mainsnak": {"snaktype": "value", "datavalue": {"value": {"time": stamp}}}}


def unknown(rank="normal"):
    """Wikidata's "unknown value" — a snak with no datavalue at all."""
    return {"rank": rank, "mainsnak": {"snaktype": "somevalue"}}


CASES = []


def case(name):
    def take(fn):
        CASES.append((name, fn))
        return fn
    return take


# --------------------------------------------------------------------------- best()

@case("best: a preferred statement hides the normal ones")
def _():
    e = entity(P31=[item("Q1"), item("Q2", rank="preferred"), item("Q3")])
    assert [s["mainsnak"]["datavalue"]["value"]["id"] for s in bp.best(e, "P31")] == ["Q2"]


@case("best: deprecated statements are never returned")
def _():
    e = entity(P31=[item("Q1", rank="deprecated"), item("Q2")])
    assert [s["mainsnak"]["datavalue"]["value"]["id"] for s in bp.best(e, "P31")] == ["Q2"]


@case("best: a deprecated statement does not count as preferred either")
def _():
    e = entity(P31=[item("Q1", rank="deprecated")])
    assert bp.best(e, "P31") == []


@case("best: unknown-value snaks are dropped, not returned as empty")
def _():
    e = entity(P31=[unknown(), item("Q2")])
    assert [s["mainsnak"]["datavalue"]["value"]["id"] for s in bp.best(e, "P31")] == ["Q2"]


@case("best: a missing property is empty, not an error")
def _():
    assert bp.best(entity(), "P31") == []
    assert bp.best({}, "P31") == []


# ------------------------------------------------------------------------- values()

@case("values: keeps statement order and drops repeats")
def _():
    e = entity(P361=[item("Q1"), item("Q2"), item("Q1")])
    assert bp.values(e, "P361") == ["Q1", "Q2"]


@case("values: ignores statements that do not point at an item")
def _():
    e = entity(P361=[text("just a string"), item("Q2")])
    assert bp.values(e, "P361") == ["Q2"]


# ------------------------------------------------------------------------ year_of()

@case("year_of: reads the year out of an ordinary timestamp")
def _():
    assert bp.year_of("+1865-01-01T00:00:00Z") == "1865"


@case("year_of: strips leading zeros")
def _():
    assert bp.year_of("+0800-01-01T00:00:00Z") == "800"


@case("year_of: keeps the sign on a date BCE")
def _():
    assert bp.year_of("-0044-03-15T00:00:00Z") == "-44"


@case("year_of: an unknown-value hash is not a date")
def _():
    assert bp.year_of("t7b2e8f19a4c0d1e5") is None
    assert bp.year_of("") is None


# ---------------------------------------------------------------- heritage_listing()

def listing(graph, roots, expect_fetches=None):
    """Run heritage_listing over a fixed graph, with no network underneath it."""
    fetches = []

    def offline(qids, props, **extra):
        fetches.append(sorted(qids))
        return {q: graph[q] for q in qids if q in graph}

    real, bp.entities = bp.entities, offline
    try:
        found = bp.heritage_listing(roots, {q: graph[q] for q in roots})
    finally:
        bp.entities = real
    if expect_fetches is not None:
        assert fetches == expect_fetches, fetches
    return found


@case("heritage: an item carrying its own P757 needs no climbing at all")
def _():
    graph = {"Q1": entity(P757=[text("421")])}
    assert listing(graph, ["Q1"], expect_fetches=[]) == {"Q1": {"id": "Q1", "ref": "421"}}


@case("heritage: one hop up a part-of chain")
def _():
    graph = {"Q1": entity(P361=[item("Q2")]), "Q2": entity(P757=[text("668")])}
    assert listing(graph, ["Q1"]) == {"Q1": {"id": "Q2", "ref": "668"}}


@case("heritage: two hops, the depth the old SPARQL query reached")
def _():
    graph = {"Q1": entity(P361=[item("Q2")]),
             "Q2": entity(P361=[item("Q3")]),
             "Q3": entity(P757=[text("668")])}
    assert listing(graph, ["Q1"]) == {"Q1": {"id": "Q3", "ref": "668"}}


@case("heritage: three hops is out of reach, exactly as it was before")
def _():
    graph = {"Q1": entity(P361=[item("Q2")]),
             "Q2": entity(P361=[item("Q3")]),
             "Q3": entity(P361=[item("Q4")]),
             "Q4": entity(P757=[text("668")])}
    assert listing(graph, ["Q1"]) == {}


@case("heritage: every parent is followed, not only the first")
def _():
    # The regression this file was written for: an item part of a city *and* of the
    # heritage site, with the city listed first.
    graph = {"Q1": entity(P361=[item("Q2"), item("Q3")]),
             "Q2": entity(),                      # the city — no listing
             "Q3": entity(P757=[text("668")])}    # the site
    assert listing(graph, ["Q1"]) == {"Q1": {"id": "Q3", "ref": "668"}}


@case("heritage: parents of a second generation are all followed too")
def _():
    graph = {"Q1": entity(P361=[item("Q2")]),
             "Q2": entity(P361=[item("Q3"), item("Q4")]),
             "Q3": entity(),
             "Q4": entity(P757=[text("668")])}
    assert listing(graph, ["Q1"]) == {"Q1": {"id": "Q4", "ref": "668"}}


@case("heritage: an item that is part of nothing listed comes back empty")
def _():
    graph = {"Q1": entity(P361=[item("Q2")]), "Q2": entity()}
    assert listing(graph, ["Q1"]) == {}


@case("heritage: each level of parents is fetched in one batch, and never a level too far")
def _():
    graph = {"Q1": entity(P361=[item("Q3")]),
             "Q2": entity(P361=[item("Q4")]),
             "Q3": entity(P361=[item("Q5")]),
             "Q4": entity(P361=[item("Q6")]),
             "Q5": entity(P361=[item("Q7")]),   # a third hop that must not be fetched
             "Q6": entity(),
             "Q7": entity(P757=[text("668")])}
    # Two fetches for two levels of climbing, each covering both roots at once.
    listing(graph, ["Q1", "Q2"], expect_fetches=[["Q3", "Q4"], ["Q5", "Q6"]])


def main():
    failed = 0
    for name, fn in CASES:
        try:
            fn()
        except AssertionError as error:
            failed += 1
            print(f"FAIL  {name}")
            if error.args:
                print(f"      {error}")
        else:
            print(f"ok    {name}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
