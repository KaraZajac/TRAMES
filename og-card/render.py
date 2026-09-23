#!/usr/bin/env python3
"""Stamp the version line and tagline onto the site's Open Graph card.

    ./og-card/render.py --version v1.2.3                       # writes docs/og.png
    ./og-card/render.py --version v1.2.3 --tagline "Directional cones, not circles · Offline by default"
    ./og-card/render.py --make-base                            # (re)build og-card/base.png from docs/og.png

og-card/base.png is the card with its two variable strings erased — the wordmark, the
route illustration and the background tints are the designed artwork and never change.
It lives here rather than in docs/ because docs/ is published as-is: the server's
site-deploy rsyncs the whole directory, so anything placed there goes live.
The two strings are drawn with the site's own fonts (docs/fonts/*.woff2, converted in
memory) at the positions measured off the original card, so a version bump is a
one-line command rather than an image-editing session. Every release before this one
hand-edited the PNG; the card was still saying v1.1.4 at v1.2.3.
"""
import argparse, io, os
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(os.path.dirname(HERE), "docs")
CARD, BASE = os.path.join(DOCS, "og.png"), os.path.join(HERE, "base.png")

# Measured off the v1.1.4 card (1200x630): where the two strings sit and what they wear.
# The tagline is three segments — "left · right" — laid out by rule (24 px either side of
# the dot) so a longer right-hand phrase simply extends rightwards, as it did in the CSS.
TAGLINE = dict(box=(60, 452, 662, 496), ink_x=72, baseline=481, gap=24, font="inter-latin.woff2", size=26.2, fill="#5c5f77")
VERSION = dict(box=(716, 556, 1150, 600), ink_x=735, baseline=585, font="jetbrains-mono-latin.woff2", size=22, fill="#5c5f77")
SUFFIX = " · Android · open source"


def font(name, size):
    t = TTFont(os.path.join(DOCS, "fonts", name)); t.flavor = None
    buf = io.BytesIO(); t.save(buf); buf.seek(0)
    f = ImageFont.truetype(buf, size)
    try:
        f.set_variation_by_axes([400])          # Regular; both fonts are variable-weight
    except Exception:
        pass
    return f


def erase(im, box):
    """Fill a box by interpolating each column between the rows just above and below it,
    which follows the background's soft tints instead of stamping a flat rectangle."""
    x0, y0, x1, y1 = box
    px = im.load()
    for x in range(x0, x1):
        top, bot = px[x, y0 - 1], px[x, y1]
        for y in range(y0, y1):
            t = (y - y0 + 1) / (y1 - y0 + 1)
            px[x, y] = tuple(round(top[i] + (bot[i] - top[i]) * t) for i in range(3))


def make_base():
    im = Image.open(CARD).convert("RGB")
    erase(im, TAGLINE["box"]); erase(im, VERSION["box"])
    im.save(BASE, optimize=True); print(f"wrote {BASE}")


def ink_extent(f, text):
    """Left and right edge of the rendered ink relative to the drawing origin. Measured by
    rendering, because getbbox() pads narrow glyphs (the middle dot reports 7 px of ink
    for a 3 px dot), and the tagline's gaps are set from the dot's true edges."""
    w = int(f.getlength(text)) + 64
    probe = Image.new("L", (w, 96), 0)
    ImageDraw.Draw(probe).text((32, 64), text, font=f, fill=255, anchor="ls")
    cols = [x for x in range(w) if any(probe.getpixel((x, y)) > 64 for y in range(96))]
    return cols[0] - 32, cols[-1] - 32


def draw_ink(d, f, text, ink_x, baseline, fill):
    """Draw so the glyphs' ink starts exactly at ink_x; return the ink's right edge."""
    x0, x1 = ink_extent(f, text)
    d.text((ink_x - x0, baseline), text, font=f, fill=fill, anchor="ls")
    return ink_x + (x1 - x0)


def render(version, tagline, out):
    im = Image.open(BASE).convert("RGB")
    d = ImageDraw.Draw(im)
    f = font(TAGLINE["font"], TAGLINE["size"])
    left, _, right = tagline.partition("·")
    x = draw_ink(d, f, left.strip(), TAGLINE["ink_x"], TAGLINE["baseline"], TAGLINE["fill"])
    x = draw_ink(d, f, "·", x + TAGLINE["gap"], TAGLINE["baseline"], TAGLINE["fill"])
    draw_ink(d, f, right.strip(), x + TAGLINE["gap"], TAGLINE["baseline"], TAGLINE["fill"])
    draw_ink(d, font(VERSION["font"], VERSION["size"]), version + SUFFIX, VERSION["ink_x"], VERSION["baseline"], VERSION["fill"])
    im.save(out, optimize=True); print(f"wrote {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", help="e.g. v1.2.3")
    ap.add_argument("--tagline", default="Directional cones, not circles · Offline by default")
    ap.add_argument("--make-base", action="store_true")
    ap.add_argument("-o", "--out", default=CARD)
    a = ap.parse_args()
    if a.make_base:
        make_base()
    if a.version:
        if not os.path.exists(BASE):
            raise SystemExit("no og-card/base.png — run with --make-base first (from a card whose strings are current)")
        render(a.version, a.tagline, a.out)
