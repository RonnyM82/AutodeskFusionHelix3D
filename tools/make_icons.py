"""Draw the Variable Pitch Helix command icons.

Run it from anywhere:

    python tools/make_icons.py

It writes resources/variable/16x16.png, 32x32.png and 64x64.png, and beside
each one an @2x twin at double the pixels. It also fills in the @2x files the
hand drawn constant pitch icon in resources/ needs, without touching the three
hand drawn originals; see constant_hidpi for how.

Add --compare to also write a side by side strip of both icons at 4x on a
Fusion dark background, so you can check the pair still look related after a
change. That goes to the system temp folder, not into the repository.

How the drawing works
---------------------
The curve is the add-in's own variable pitch helix: the module is imported with
a stub in place of adsk, so the icon is a real helix rather than a drawing of
one. Change PROFILE and the icon changes the way the command would.

It is then projected straight down the Y axis with the circle squashed
vertically, stroked by stamping discs along it at ten to sixteen times the
final size, and box filtered down for the anti aliasing.

The numbers in FITTED were not chosen by eye. They came from a coordinate
descent against the existing 64x64 icon, minimising the mean difference in
alpha, which is why the two sit together properly. Leave them alone unless you
are deliberately restyling both icons.

The two colours are the depth cue the original uses. Each coil is drawn twice:
the half going round the back in a muted blue grey, then the half facing you in
the bright blue painted over the top.

Why every icon is written twice
-------------------------------
Fusion is Qt underneath, and when the screen puts two real pixels where the
interface asked for one, as a Retina Mac or a 4K laptop does, Qt looks first
for the same file name with @2x on the end. If it is not there it stretches the
ordinary file up instead, and that is what makes the ribbon look grainy. So
32x32.png is the drawing at 32 pixels and 32x32@2x.png is the same drawing at
64. Same picture, same apparent size on screen, twice the pixels.
"""
import argparse
import math
import os
import struct
import sys
import tempfile
import types
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Fitted against resources/64x64.png. All in 64 pixel units; everything is
# scaled by size/64 for the smaller icons.
FITTED = {
    'cx': 31.25,        # centre of the helix across the canvas
    'cy': 53.5,         # where the bottom of the curve sits before centring
    'radius': 20.25,    # of the coil
    'squash': 0.325,    # how flat the circle is seen: minor over major axis
    'climb': 44.625,    # total rise, top of the curve to the bottom
    'start': 0.3927,    # start angle, 22.5 degrees
    'stroke': 3.725,    # line width
}

# Thinner line at the small sizes, or the tight coils at the bottom merge.
# Keyed on the size the icon appears at, not on how many pixels it is drawn
# with, so an @2x file keeps the line weight of the size it stands in for.
STROKE_SCALE = {64: 1.00, 32: 0.95, 16: 0.80}
# Keyed on pixels actually drawn. Bigger canvases need less oversampling.
SUPERSAMPLE = {128: 8, 64: 10, 32: 12, 16: 16}

# Four coils, tight at the bottom and opening out towards the top. Pitch here
# is relative: the whole profile is scaled to the climb above, so only the
# ratios between these matter.
PROFILE = [{'turns': 1.0, 'pitch': 0.30, 'radius': 1.0},
           {'turns': 1.0, 'pitch': 0.62, 'radius': 1.0},
           {'turns': 1.0, 'pitch': 1.18, 'radius': 1.0},
           {'pitch': 1.90, 'radius': 1.0}]

# The constant pitch icon, the one drawn by hand, fitted the same way and for
# the same reason: there is no drawing file behind it, so the only way to get a
# 128 pixel version of it was to find the helix that draws it. Same coordinate
# descent on mean alpha difference against resources/64x64.png, which it lands
# within about five per cent of. Even pitch, so the profile is one long run.
CONST_FITTED = {
    'cx': 31.9274,
    'cy': 53.5,
    'radius': 19.9602,
    'squash': 0.3341,
    'climb': 43.2929,
    'start': 0.7324,
    'stroke': 3.9626,
    'turns': 2.816,
}
CONST_PROFILE = [{'turns': CONST_FITTED['turns'], 'pitch': 1.0, 'radius': 1.0},
                 {'pitch': 1.0, 'radius': 1.0}]

NEAR = (0x5A, 0xAA, 0xFF)   # the half of the coil facing you
FAR = (0x96, 0xAE, 0xC8)    # the half going round the back
DARK_UI = (0x2B, 0x2B, 0x2B)


def load_addin():
    """Import Helix3D for its pitch ramp, with a stub standing in for adsk."""
    for name in ('adsk', 'adsk.core', 'adsk.fusion'):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules['adsk'].core = sys.modules['adsk.core']
    sys.modules['adsk'].fusion = sys.modules['adsk.fusion']

    class _App:
        def log(self, *a):
            pass

        @staticmethod
        def get():
            return _App()

        userInterface = None

    sys.modules['adsk.core'].Application = _App
    for n in ('CustomEventHandler', 'CommandEventHandler', 'InputChangedEventHandler',
              'CommandCreatedEventHandler', 'SelectionEventHandler',
              'ApplicationCommandEventHandler', 'MarkingMenuEventHandler'):
        setattr(sys.modules['adsk.core'], n, object)
    sys.modules['adsk.fusion'].CustomFeatureEventHandler = object
    sys.path.insert(0, ROOT)
    import Helix3D
    return Helix3D


# --------------------------------------------------------------------------
# PNG in and out, so the tool needs nothing installed
# --------------------------------------------------------------------------
def write_png(path, w, h, rows):
    raw = b''.join(b'\x00' + bytes(rows[y]) for y in range(h))

    def chunk(tag, data):
        c = tag + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n'
                + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0))
                + chunk(b'IDAT', zlib.compress(raw, 9))
                + chunk(b'IEND', b''))


def read_png(path):
    """Enough of a reader for our own 8 bit RGBA icons."""
    d = open(path, 'rb').read()
    pos, idat, w, h = 8, b'', None, None
    while pos < len(d):
        ln = struct.unpack('>I', d[pos:pos + 4])[0]
        typ, data = d[pos + 4:pos + 8], d[pos + 8:pos + 8 + ln]
        if typ == b'IHDR':
            w, h = struct.unpack('>II', data[:8])
        elif typ == b'IDAT':
            idat += data
        pos += 12 + ln
    raw, out, prev, i = zlib.decompress(idat), [], bytearray(w * 4), 0
    for _ in range(h):
        f = raw[i]
        i += 1
        line = bytearray(raw[i:i + w * 4])
        i += w * 4
        for x in range(w * 4):
            a = line[x - 4] if x >= 4 else 0
            b = prev[x]
            c = prev[x - 4] if x >= 4 else 0
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 255
        prev = line
        out.append([tuple(line[x * 4:x * 4 + 4]) for x in range(w)])
    return w, h, out


# --------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------
def coverage(size, pts, stroke, ss):
    """How much of each final pixel the stroke covers, 0 to 1."""
    W = size * ss
    grid = [bytearray(W) for _ in range(W)]
    r = stroke * ss / 2.0
    reach, r2 = int(math.ceil(r)) + 1, r * r
    for px, py in pts:
        cx, cy = px * ss, py * ss
        for yy in range(max(int(cy - reach), 0), min(int(cy + reach) + 1, W)):
            dy = yy + 0.5 - cy
            if dy * dy > r2:
                continue
            half = math.sqrt(r2 - dy * dy)
            x0 = max(int(cx - half), 0)
            x1 = min(int(cx + half) + 1, W)
            if x1 > x0:
                # One slice, not a Python loop per pixel. Same result, and it
                # is what keeps the 128 pixel icons down to seconds.
                grid[yy][x0:x1] = b'\x01' * (x1 - x0)
    n = float(ss * ss)
    return [[sum(sum(grid[yy][x * ss:x * ss + ss])
                 for yy in range(y * ss, y * ss + ss)) / n
             for x in range(size)] for y in range(size)]


def paint(size, layers, stroke, ss):
    """Composite the layers in order, each over the last."""
    acc = [[(0.0, 0.0, 0.0, 0.0)] * size for _ in range(size)]
    for pts, rgb in layers:
        cov = coverage(size, pts, stroke, ss)
        for y in range(size):
            for x in range(size):
                a = cov[y][x]
                if a <= 0.0:
                    continue
                pr, pg, pb, pa = acc[y][x]
                na = a + pa * (1 - a)
                acc[y][x] = ((rgb[0] * a + pr * pa * (1 - a)) / na,
                             (rgb[1] * a + pg * pa * (1 - a)) / na,
                             (rgb[2] * a + pb * pa * (1 - a)) / na, na)
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            r, g, b, a = acc[y][x]
            row += bytes((int(round(r)), int(round(g)), int(round(b)), int(round(a * 255))))
        rows.append(row)
    return rows


def split_depth(samples):
    """Cut the curve where it crosses between the far and near halves, so the
    near half can be painted over the far one."""
    far, near, run, run_far = [], [], [], None
    for pt, is_far in samples:
        if run_far is None:
            run_far = is_far
        if is_far != run_far:
            run.append(pt)
            (far if run_far else near).append(run)
            run, run_far = [pt], is_far
        run.append(pt)
    if run:
        (far if run_far else near).append(run)
    return far, near


def sample(H, profile, size, stroke, fit=FITTED):
    """Project the helix into the canvas and say which half each point is on."""
    s = size / 64.0
    cx, cy = fit['cx'] * s, fit['cy'] * s
    R, climb = fit['radius'] * s, fit['climb'] * s
    xs, ps, rs = H._station_axes(profile)
    pitch, radius = H._Ramp(xs, ps, True), H._Ramp(xs, rs, True)
    k, total = climb / pitch.total(), xs[-1]
    steps = max(2400, size * 160)
    pts = []
    for i in range(steps + 1):
        u = total * i / steps
        a = fit['start'] + 2 * math.pi * u
        rr = radius.value(u) * R
        y = cy - (pitch.integral(u) * k + fit['squash'] * rr * math.sin(a))
        pts.append(((cx + rr * math.cos(a), y), math.sin(a) > 0.0))
    ys = [p[1] for p, _ in pts]
    shift = (size - (max(ys) - min(ys) + stroke)) / 2.0 - (min(ys) - stroke / 2)
    return [((x, y + shift), f) for (x, y), f in pts]


def build(H, profile, size, scale=1, fit=FITTED, ss=None):
    """Draw the icon for `size`, using `scale` pixels per pixel of it."""
    px = size * scale
    stroke = fit['stroke'] * (px / 64.0) * fit.get('thin', STROKE_SCALE)[size]
    far, near = split_depth(sample(H, profile, px, stroke, fit))
    layers = [(seg, FAR) for seg in far] + [(seg, NEAR) for seg in near]
    return paint(px, layers, stroke, ss or SUPERSAMPLE[px])


def compare_strip(path, scale=4, pad=12):
    """Both icons at every size, on a Fusion dark background."""
    files = []
    for size in (64, 32, 16):
        files.append(os.path.join(ROOT, 'resources', '%dx%d.png' % (size, size)))
        files.append(os.path.join(ROOT, 'resources', 'variable', '%dx%d.png' % (size, size)))
    icons = [read_png(f) for f in files]
    W = pad + sum(w * scale + pad for w, _, _ in icons)
    Hh = pad * 2 + 64 * scale
    canvas = [[DARK_UI] * W for _ in range(Hh)]
    x0 = pad
    for w, h, px in icons:
        oy = pad + (64 * scale - h * scale) // 2
        for y in range(h * scale):
            for x in range(w * scale):
                r, g, b, a = px[y // scale][x // scale]
                f = a / 255.0
                canvas[oy + y][x0 + x] = tuple(
                    int(c * f + DARK_UI[i] * (1 - f)) for i, c in enumerate((r, g, b)))
        x0 += w * scale + pad
    rows = []
    for y in range(Hh):
        row = bytearray()
        for x in range(W):
            row += bytes(canvas[y][x] + (255,))
        rows.append(row)
    write_png(path, W, Hh, rows)


def constant_hidpi(H):
    """Give the hand drawn constant pitch icon its @2x files.

    Two of the three are free. An @2x file is the same picture with twice the
    pixels, and the next hand drawn file up is already exactly that: 64x64.png
    is a 64 pixel drawing of the icon, which is precisely what 32x32@2x.png has
    to be. Those are copied across byte for byte rather than resampled, so the
    ribbon on a Retina Mac shows the drawing itself and not a stretched one.

    The third has nothing above it to copy, so 64x64@2x.png is drawn from
    CONST_FITTED instead, the helix found by fitting to the hand drawn 64x64.
    It is the only generated file in the constant pitch set. The three hand
    drawn originals are left exactly as they are.
    """
    done = []
    for src, dst in (('32x32.png', '16x16@2x.png'), ('64x64.png', '32x32@2x.png')):
        s_path = os.path.join(ROOT, 'resources', src)
        d_path = os.path.join(ROOT, 'resources', dst)
        with open(s_path, 'rb') as f:
            data = f.read()
        if not (os.path.exists(d_path) and open(d_path, 'rb').read() == data):
            with open(d_path, 'wb') as f:
                f.write(data)
        done.append('resources/%s, copied from %s' % (dst, src))

    out = os.path.join(ROOT, 'resources', '64x64@2x.png')
    write_png(out, 128, 128, build(H, CONST_PROFILE, 64, 2, CONST_FITTED))
    done.append('resources/64x64@2x.png, drawn at 128')
    return done


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--compare', action='store_true',
                    help='also write a side by side strip of both icons to the temp folder')
    args = ap.parse_args()

    H = load_addin()
    for size in (64, 32, 16):
        for scale in (1, 2):
            px = size * scale
            name = '%dx%d%s.png' % (size, size, '@2x' if scale == 2 else '')
            out = os.path.join(ROOT, 'resources', 'variable', name)
            write_png(out, px, px, build(H, PROFILE, size, scale))
            print('wrote %s' % os.path.relpath(out, ROOT))

    for name in constant_hidpi(H):
        print('wrote %s' % name)

    if args.compare:
        strip = os.path.join(tempfile.gettempdir(), 'helix3d-icon-compare.png')
        compare_strip(strip)
        print('wrote %s' % strip)


if __name__ == '__main__':
    main()
