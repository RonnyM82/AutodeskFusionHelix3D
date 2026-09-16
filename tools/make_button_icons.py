"""Draw the icons for the push buttons that sit in the command dialogs.

Run it from anywhere:

    python tools/make_button_icons.py

It writes resources/buttons/<name>/16x16.png, 32x32.png and 64x64.png, and an
@2x twin of each, the same way tools/make_mode_icons.py does and for the same
reason: without the @2x file Fusion stretches the 16 pixel image on a screen
that draws two device pixels per interface pixel and it goes grainy.

A button is the one input Fusion will not draw without an icon. The API
documentation is explicit about it ("If it's a button you need to specify the
resource folder"), and a button given an empty folder comes out blank, so
these are not decoration.

copy-radius: three bars of equal length with an arrow running down beside
them. It stands for the Radius column after every row has been given the same
number, which is exactly what the button does.
"""
import os

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTDIR = os.path.join(ROOT, 'resources', 'buttons')

SS = 8                                   # drawn this many times over size
SIZES = (16, 32, 64)
BLUE = (0x5A, 0xAA, 0xFF, 255)


def canvas(px):
    img = Image.new('RGBA', (px * SS, px * SS), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def bar(d, x0, x1, y, w):
    d.rectangle([x0 * SS, (y - w / 2) * SS, x1 * SS, (y + w / 2) * SS], fill=BLUE)


def arrow_down(d, x, y0, y1, w, head):
    """A shaft with a solid head on the bottom. Two strokes at sixteen pixels
    turn to mush, so the head is a filled triangle rather than a chevron."""
    d.rectangle([(x - w / 2) * SS, y0 * SS, (x + w / 2) * SS, (y1 - head) * SS], fill=BLUE)
    d.polygon([((x - head * 0.72) * SS, (y1 - head) * SS),
               ((x + head * 0.72) * SS, (y1 - head) * SS),
               (x * SS, y1 * SS)], fill=BLUE)


def glyph(name, px, scale=1):
    """The glyph as it looks at `px`, written with `scale` pixels per pixel."""
    img, d = canvas(px)
    S = float(px)
    w = max(0.9, S * 0.085)              # bar thickness, in final pixels

    if name == 'copy-radius':
        for y in (0.20, 0.50, 0.80):
            bar(d, S * 0.42, S * 0.94, S * y, w)
        arrow_down(d, S * 0.16, S * 0.14, S * 0.88, max(0.9, S * 0.075), S * 0.26)

    return img.resize((px * scale, px * scale), Image.LANCZOS)


BUTTONS = ('copy-radius',)

if __name__ == '__main__':
    for name in BUTTONS:
        folder = os.path.join(OUTDIR, name)
        os.makedirs(folder, exist_ok=True)
        for px in SIZES:
            for scale in (1, 2):
                out = '%dx%d%s.png' % (px, px, '@2x' if scale == 2 else '')
                glyph(name, px, scale).save(os.path.join(folder, out), optimize=True)
        print('wrote %s' % os.path.relpath(folder, ROOT))
