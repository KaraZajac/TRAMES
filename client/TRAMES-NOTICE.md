# TRAMES — attribution and licensing

**TRAMES is a fork of [OsmAnd](https://github.com/osmandapp/OsmAnd). It is not OsmAnd,
and it is not endorsed by or affiliated with OsmAnd BV.**

OsmAnd – OSM Automated Navigation Directions
Copyright © 2010–2026 OsmAnd BV (Amstelveen, Netherlands — KvK 62066714, BTW 854627704B01)

## Licensing

| Component | License |
|---|---|
| OsmAnd source code (and TRAMES modifications to it) | **GPLv3** |
| OsmAnd UI design, layouts and icons (`OsmAnd/res`) | **CC-BY-NC-ND 4.0** |
| TRAMES-original artwork (`ic_trames_*`) | see below |
| Map data | **ODbL** (OpenStreetMap contributors) |
| ALPR camera data | **ODbL** (OpenStreetMap contributors, via Overpass) |

TRAMES is distributed under **GPLv3**, and its full source is published. As required,
modifications to OsmAnd's code are made available under the same terms.

## What the CC-BY-NC-ND artwork means in practice

OsmAnd's `res/` tree — explicitly including **layouts as well as icons** — is licensed
CC-BY-NC-ND 4.0. That is **No Derivatives** and **NonCommercial**, so TRAMES observes
two hard rules:

1. **TRAMES is and remains non-commercial.** No sale, no advertising, no paid tier, no
   donations attached to it. It is given away.
2. **In `res/`, we add — we do not edit.** New TRAMES functionality ships as *new*
   layout and drawable files. Existing OsmAnd resources are either used unmodified
   (permitted: verbatim redistribution with attribution) or replaced outright by our
   own work. They are never altered in place, because an altered OsmAnd layout would be
   a derivative work of ND-licensed material.

Branding assets (`ic_trames_background`, `ic_trames_foreground`, `ic_trames_monochrome`,
`mipmap*/icon_trames`) are **original work created for TRAMES**. They are not derived
from, based on, or traced from any OsmAnd artwork. OsmAnd's own icon files remain in the
tree unmodified and unused by this build.

> OsmAnd's LICENSE additionally warns: *"Please be aware that some artwork uses a
> proprietary license"* — without enumerating which. This is an unresolved risk. Before
> any public release, audit `OsmAnd/res` for third-party-branded assets (partner logos,
> integration marks) and remove them.

## Why the fork exists

TRAMES adds routing that avoids automated licence-plate readers (ALPRs). Camera
positions come from OpenStreetMap surveillance tagging via the Overpass API; routing
runs on a self-hosted GraphHopper instance with camera field-of-view cones baked into
the graph as custom areas.

Everything else — offline maps, search, turn-by-turn guidance, voice, the OSM editor —
is OsmAnd's work, and the credit for it is theirs.
