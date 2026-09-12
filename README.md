# assets.core

The world data behind [zhang-en-yao.github.io](https://zhang-en-yao.github.io): the atlas
the travel map draws, and the star charts in its polar bands. None of it is written by
hand. Every file here is fetched from a public source and re-encoded, and nothing in it is
translated, reworded or interpreted — the site joins it by key and renders it.

The site pins one tag of this repository, so nothing published here reaches the live page
until that pin moves.

```
https://cdn.jsdelivr.net/gh/Zhang-En-Yao/assets.core@<tag>/atlas/countries-50m.json
```

## What is here

| Path | Source | Notes |
| --- | --- | --- |
| `atlas/countries-50m.json` | [world-atlas](https://github.com/topojson/world-atlas) (Natural Earth 1:50m) | first paint, every page |
| `atlas/countries-10m.json` | [Natural Earth 1:10m](https://github.com/nvkelso/natural-earth-vector) | fetched only when the map is zoomed in |
| `atlas/marine-areas.json` | Natural Earth 1:10m | `areas` = named seas, gulfs, straits; `borders` = maritime boundary indicators |
| `sky/stars.json` | [AT-HYG v4.0](https://codeberg.org/astronexus/athyg) + [d3-celestial](https://github.com/ofrohn/d3-celestial) | stars to magnitude 6, and the IAU constellation figures |

`MANIFEST.json` records what each build produced. It is what the drift check compares
against, and the quickest way to see whether a rebuild actually changed anything.

## Rebuilding

Each script fetches from upstream and writes into this repository. They are slow (the star
catalogue is a 14 MB download; the atlas is 25 MB of GeoJSON) and they need the network.

```
python3 build/build-atlas.py     # countries-10m, marine-areas  — needs node, for the TopoJSON tools
python3 build/build-stars.py     # stars.json
python3 build/validate.py        # check everything, then update MANIFEST.json
```

`countries-50m.json` is not built here; it is world-atlas's own published file, copied in.

## Licences

Natural Earth is public domain. AT-HYG is CC BY-SA 4.0 (astronexus). The constellation
figures are BSD-3-Clause (Olaf Frohn, d3-celestial). The site credits all three under its
map.

## How a change gets published

1. a workflow (or you) runs a build script
2. `validate.py` checks the shape, the invariants and the drift, and fails the run rather
   than committing something broken
3. the change is committed and tagged `YYYY-MM-DD`
4. the site's `assets/js/shared/assets.js` is pointed at the new tag — a separate, manual
   step, so a bad build cannot reach the live page on its own

Step 3 is why tags matter: jsDelivr caches a tag forever, and caches a branch for days
without telling you. Never point the site at `@main`.
