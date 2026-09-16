"""Draw the little icons that sit beside each mode in the Mode dropdown.

Run it from anywhere:

    python tools/make_mode_icons.py

It writes resources/modes/<mode>/16x16.png, 32x32.png and 64x64.png. Fusion
only shows these when the dropdown uses LabeledIconDropDownStyle; with the
plain text style the icon argument is accepted and then ignored.

The glyphs say what shape the mode makes and which two numbers you type for
it. Three of the six make the same straight helix and differ only by the
measuring arrow beside it, so at 16 pixels they read as a family rather than
as six separate things. That is the honest answer: the modes differ in what
you hand Fusion, not in what comes out.

Drawn at eight times size and filtered down, on a transparent ground in the
same blue as the command icons so they sit on the dark dropdown.
"""
import math
import os
import sys

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTDIR = os.path.join(ROOT, 'resources', 'modes')

SS = 8
SIZES = (16, 32, 64)
BLUE = (0x5A, 0xAA, 0xFF, 255)
FAINT = (0x5A, 0xAA, 0xFF, 150)


def canvas(px):
    img = Image.new('RGBA', (px * SS, px * SS), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def polyline(d, pts, w, colour=BLUE):
    d.line([c for p in pts for c in (p[0] * SS, p[1] * SS)],
           fill=colour, width=max(1, int(round(w * SS))), joint='curve')


def bar(d, x, y0, y1, half):
    """A solid bar standing for a measurement. An arrow with heads on it turns
    to mush at sixteen pixels; a bar of the right length still reads."""
    d.rectangle([(x - half) * SS, y0 * SS, (x + half) * SS, y1 * SS], fill=BLUE)


def coil(d, cx, ytop, ybot, r, turns, w, squash=0.42, start=0.5):
    """A straight helix, seen slightly from above."""
    rise = (ybot - ytop) - 2 * squash * r
    pts = []
    for i in range(241):
        t = i / 240.0
        a = start + 2 * math.pi * turns * t
        pts.append((cx + r * math.cos(a),
                    ybot - squash * r - rise * t - squash * r * math.sin(a)))
    polyline(d, pts, w)
    return rise / turns          # one coil gap, for the pitch arrow


def flat_spiral(d, cx, cy, r0, r1, turns, w, squash=0.46):
    pts = []
    for i in range(281):
        t = i / 280.0
        a = 2 * math.pi * turns * t
        r = r0 + (r1 - r0) * t
        pts.append((cx + r * math.cos(a), cy + squash * r * math.sin(a)))
    polyline(d, pts, w)


def curved_coil(d, box, turns, r, w, squash=0.45):
    """A helix wound around a bent path, which is what the path modes make."""
    x0, y0, x1, y1 = box
    bulge = (x1 - x0) * 0.30
    path = []
    for i in range(121):
        t = i / 120.0
        path.append((x0 + bulge * math.sin(math.pi * t) + r,
                     y0 + (y1 - y0) * t))
    polyline(d, path, w * 0.6, FAINT)
    pts = []
    for i in range(321):
        t = i / 320.0
        j = t * (len(path) - 1)
        k = min(int(j), len(path) - 2)
        f = j - k
        px = path[k][0] + (path[k + 1][0] - path[k][0]) * f
        py = path[k][1] + (path[k + 1][1] - path[k][1]) * f
        tx = path[k + 1][0] - path[k][0]
        ty = path[k + 1][1] - path[k][1]
        n = math.hypot(tx, ty) or 1.0
        nx, ny = -ty / n, tx / n
        a = 2 * math.pi * turns * t
        pts.append((px + r * math.cos(a) * nx,
                    py + r * math.cos(a) * ny - squash * r * math.sin(a)))
    polyline(d, pts, w)


# --------------------------------------------------------------------------
# One function per mode, all working in a 0..1 box scaled to the icon size
# --------------------------------------------------------------------------
def glyph(name, px):
    img, d = canvas(px)
    S = float(px)
    w = max(0.85, S * 0.055)             # stroke, in final pixels
    half = max(0.55, S * 0.045)          # half the width of a measuring bar

    if name == 'spiral':
        flat_spiral(d, S * 0.5, S * 0.5, S * 0.05, S * 0.44, 2.4, w)

    elif name in ('path-pitch', 'path-revolutions'):
        right = S * 0.74 if name == 'path-pitch' else S * 0.94
        curved_coil(d, (S * 0.08, S * 0.10, right, S * 0.90), 3.0, S * 0.16, w)
        if name == 'path-pitch':
            bar(d, S * 0.88, S * 0.38, S * 0.62, half)

    else:
        pitch_left = name == 'height-pitch'
        cx = S * 0.44 if not pitch_left else S * 0.50
        r = S * 0.30 if not pitch_left else S * 0.25
        gap = coil(d, cx, S * 0.10, S * 0.90, r, 2.5, w)
        if name in ('revolutions-pitch', 'height-pitch'):
            x, mid = (S * 0.88 if not pitch_left else S * 0.08), S * 0.50
            bar(d, x, mid - gap / 2, mid + gap / 2, half)
        if name in ('revolutions-height', 'height-pitch'):
            bar(d, S * 0.90, S * 0.11, S * 0.89, half)

    return img.resize((px, px), Image.LANCZOS)


MODES = ('revolutions-pitch', 'revolutions-height', 'height-pitch',
         'spiral', 'path-pitch', 'path-revolutions')

if __name__ == '__main__':
    for name in MODES:
        folder = os.path.join(OUTDIR, name)
        os.makedirs(folder, exist_ok=True)
        for px in SIZES:
            glyph(name, px).save(os.path.join(folder, '%dx%d.png' % (px, px)), optimize=True)
        print('wrote %s' % os.path.relpath(folder, ROOT))
