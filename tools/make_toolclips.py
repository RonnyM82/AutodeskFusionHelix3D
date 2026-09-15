"""Draw the tool clip images for the Variable Pitch Helix dialog.

Run it from anywhere:

    python tools/make_toolclips.py

It writes resources/toolclips/*.png. Fusion shows one of these inside the
tooltip, under the title, once the pointer rests on a control: set it with
CommandInput.toolClipFilename. They have to be real PNG files on disk, which
is why they ship in the add-in folder rather than being drawn at run time.

They are deliberately wordy-free: the tooltip description does the explaining,
the picture only has to show the shape of the thing. Each is 300 x 200, which
is the size Fusion's own tool clips use, drawn at four times that and filtered
down so the curves and the small type stay clean.

The helix curves come from the add-in's own solver, the same as the command
icon does, so a picture of a helix here is a real one.
"""
import math
import os
import sys

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_icons

H = make_icons.load_addin()
ROOT = make_icons.ROOT
OUTDIR = os.path.join(ROOT, 'resources', 'toolclips')

W, HT, SS = 300, 200, 4          # final size, and how much to oversample

# Light, because a Fusion tooltip is a light bubble even in the dark theme.
GROUND = (247, 248, 250)
INK = (43, 52, 64)
MUTED = (110, 122, 136)
HAIR = (205, 213, 222)
BLUE = (58, 145, 222)            # the near half of a coil
BLUE_FAR = (158, 180, 200)       # the half going round the back
ORANGE = (222, 122, 10)          # the part being talked about
ORANGE_FILL = (250, 227, 198)
ORANGE_EDGE = (240, 190, 140)


def font(px):
    for name in ('segoeui.ttf', 'arial.ttf', 'tahoma.ttf'):
        p = os.path.join(r'C:\Windows\Fonts', name)
        if os.path.exists(p):
            return ImageFont.truetype(p, int(px * SS))
    return ImageFont.load_default()


F_CAPTION, F_LABEL, F_SMALL = font(11), font(10), font(9)


def canvas():
    img = Image.new('RGB', (W * SS, HT * SS), GROUND)
    return img, ImageDraw.Draw(img)


def s(v):
    return int(round(v * SS))


def text(d, xy, msg, fnt=F_LABEL, fill=INK, anchor='mm'):
    d.text((s(xy[0]), s(xy[1])), msg, font=fnt, fill=fill, anchor=anchor)


def caption(d, msg):
    text(d, (W / 2, HT - 13), msg, F_CAPTION, MUTED, 'mm')


def line(d, a, b, fill, width=1.4):
    d.line([s(a[0]), s(a[1]), s(b[0]), s(b[1])], fill=fill, width=s(width))


def dashed(d, a, b, fill, width=1.0, dash=4.0, gap=3.0):
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dy)
    if n < 1e-9:
        return
    ux, uy, t = dx / n, dy / n, 0.0
    while t < n:
        e = min(t + dash, n)
        line(d, (a[0] + ux * t, a[1] + uy * t), (a[0] + ux * e, a[1] + uy * e), fill, width)
        t = e + gap


def polyline(d, pts, fill, width=1.6):
    d.line([c for p in pts for c in (s(p[0]), s(p[1]))], fill=fill,
           width=s(width), joint='curve')


def dot(d, p, r, fill, outline=None, ow=1.2):
    d.ellipse([s(p[0] - r), s(p[1] - r), s(p[0] + r), s(p[1] + r)],
              fill=fill, outline=outline, width=s(ow))


def save(img, name):
    os.makedirs(OUTDIR, exist_ok=True)
    out = os.path.join(OUTDIR, name)
    img.resize((W, HT), Image.LANCZOS).save(out, optimize=True)
    print('wrote %s' % os.path.relpath(out, ROOT))


# --------------------------------------------------------------------------
# A helix, projected the way the command icon draws one
# --------------------------------------------------------------------------
def helix(d, stations, box, smooth=True, lit=None, marks=(), numbered=False,
          squash=0.34, start_ang=0.6):
    """Draw a helix inside box (x0, y0, x1, y1). `lit` is a (from, to) range in
    turns drawn in orange over the top; `marks` are turns to put a dot on."""
    x0, y0, x1, y1 = box
    xs, ps, rs = H._station_axes(stations)
    pitch, radius = H._Ramp(xs, ps, smooth), H._Ramp(xs, rs, smooth)
    total, climb = xs[-1], pitch.total()
    rmax = max(rs)
    # Keep the helix in proportion. Take the radius from the box width, then if
    # the stack of coils is taller than the box shrink the whole thing to fit,
    # rather than squashing the height and running the coils into each other.
    R = (x1 - x0) / 2.0
    tall = (climb / rmax + 2 * squash) * R
    avail = y1 - y0
    if tall > avail:
        R *= avail / tall
        tall = avail
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0 + tall / 2.0 - squash * R

    def at(u):
        a = start_ang + 2 * math.pi * u
        rr = radius.value(u) / rmax * R
        return (cx + rr * math.cos(a),
                cy - (pitch.integral(u) / rmax * R + squash * rr * math.sin(a)))

    def run(lo, hi, near_c, far_c, width):
        n = max(24, int((hi - lo) * 56))
        seg, was = [], None
        for i in range(n + 1):
            u = lo + (hi - lo) * i / n
            far = math.sin(start_ang + 2 * math.pi * u) > 0
            if was is None:
                was = far
            p = at(u)
            if far != was and len(seg) > 1:
                polyline(d, seg + [p], far_c if was else near_c, width)
                seg, was = [seg[-1], p], far
            else:
                seg.append(p)
        if len(seg) > 1:
            polyline(d, seg, far_c if was else near_c, width)

    run(0.0, total, BLUE, BLUE_FAR, 1.7)
    if lit:
        run(lit[0], lit[1], ORANGE, ORANGE_EDGE, 2.6)
    for i, u in enumerate(marks):
        p = at(u)
        dot(d, p, 2.6, GROUND, ORANGE, 1.3)
        if numbered:
            # Push the number away from the axis, or two stations at the same
            # angle end up with their labels on top of each other.
            text(d, (p[0] + (10.0 if p[0] >= cx else -10.0), p[1] - 1),
                 str(i + 1), F_SMALL, ORANGE, 'mm')
    return at


# --------------------------------------------------------------------------
# A pitch-against-turns chart, which is where the rise comes from
# --------------------------------------------------------------------------
def pitch_chart(d, box, stations, smooth, fill=True, label_area=None):
    x0, y0, x1, y1 = box
    xs, ps, _ = H._station_axes(stations)
    ramp = H._Ramp(xs, ps, smooth)
    total, top = xs[-1], max(ps) * 1.25

    def pt(u):
        return (x0 + (x1 - x0) * u / total, y1 - (y1 - y0) * ramp.value(u) / top)

    pts = [pt(total * i / 160.0) for i in range(161)]
    if fill:
        d.polygon([c for p in [(x0, y1)] + pts + [(x1, y1)] for c in (s(p[0]), s(p[1]))],
                  fill=ORANGE_FILL)
    line(d, (x0, y1), (x1, y1), HAIR, 1.0)
    polyline(d, pts, BLUE, 1.8)
    for u in xs:
        dot(d, pt(u), 2.4, GROUND, BLUE, 1.2)
    if label_area:
        text(d, ((x0 + x1) / 2, (y1 + min(p[1] for p in pts)) / 2 + 4),
             label_area, F_SMALL, ORANGE, 'mm')
    return pt


# --------------------------------------------------------------------------
# The clips
# --------------------------------------------------------------------------
# Three turns or so keeps the coils apart at 300 x 200. These are drawn to
# read, not to be anyone's real spring.
RAMP = [{'turns': 1.0, 'pitch': 0.25, 'radius': 1.0}, {'pitch': 1.0, 'radius': 1.0}]
# 0.6 / 1.7 / 0.6 turns from a start angle of 0.3 puts all four stations well
# apart on screen; at 0.8 turns the first two landed almost on top of one
# another, because 0.8 of a turn comes back to nearly the same side.
SPRING = [{'turns': 0.6, 'pitch': 0.16, 'radius': 1.0},
          {'turns': 1.7, 'pitch': 0.80, 'radius': 1.0},
          {'turns': 0.6, 'pitch': 0.80, 'radius': 1.0},
          {'pitch': 0.16, 'radius': 1.0}]
SPRING_MARKS = (0.0, 0.6, 2.3, 2.9)
SPRING_ANGLE = 0.30
PLAIN = [{'turns': 3.0, 'pitch': 0.6, 'radius': 1.0}, {'pitch': 0.6, 'radius': 1.0}]
FLAT_END = [{'turns': 0.7, 'pitch': 0.12, 'radius': 1.0},
            {'turns': 0.5, 'pitch': 0.12, 'radius': 1.0},
            {'turns': 1.8, 'pitch': 0.60, 'radius': 1.0},
            {'pitch': 0.60, 'radius': 1.0}]
CONE = [{'turns': 3.0, 'pitch': 0.5, 'radius': 0.42}, {'pitch': 0.5, 'radius': 1.0}]
BOX = (80, 32, 220, 170)          # where a single helix is drawn


def clip_pitch():
    img, d = canvas()
    text(d, (W / 2, 20), 'pitch climbs from one station to the next', F_LABEL, INK, 'mm')
    box = (46, 44, 254, 140)
    pt = pitch_chart(d, box, RAMP, True, True, 'rise')
    a, b = pt(0.0), pt(1.0)
    # the rectangle people expect: the bigger pitch held all the way across
    dashed(d, (box[0], b[1]), (box[2], b[1]), MUTED, 1.0)
    text(d, (box[0] + 52, b[1] - 9), 'not this much', F_SMALL, MUTED, 'mm')
    text(d, (a[0] - 4, a[1] - 2), 'pitch', F_SMALL, BLUE, 'rm')
    text(d, (b[0] + 4, b[1] - 2), 'pitch', F_SMALL, BLUE, 'lm')
    text(d, (W / 2, 150), 'one turn', F_SMALL, MUTED, 'mm')
    caption(d, 'the rise is the area, so the average of the two')
    return img


def clip_turns():
    img, d = canvas()
    text(d, (W / 2, 20), 'turns between one station and the next', F_LABEL, INK, 'mm')
    lo, hi = SPRING_MARKS[1], SPRING_MARKS[2]      # the middle run of SPRING
    at = helix(d, SPRING, BOX, True, lit=(lo, hi), marks=(lo, hi),
               start_ang=SPRING_ANGLE)
    a, b = at(lo), at(hi)
    x = BOX[2] + 12                # clear of the coil, whatever size it shrank to
    line(d, (x, a[1]), (x, b[1]), ORANGE, 1.3)
    for y in (a[1], b[1]):
        line(d, (x - 4, y), (x + 4, y), ORANGE, 1.3)
    text(d, (x + 8, (a[1] + b[1]) / 2), '%.2f' % (hi - lo), F_SMALL, ORANGE, 'lm')
    caption(d, 'measured from the row above down to this one')
    return img


def clip_pitch_cell():
    img, d = canvas()
    text(d, (W / 2, 20), 'pitch is set at each station', F_LABEL, INK, 'mm')
    helix(d, SPRING, BOX, True, marks=SPRING_MARKS, numbered=True,
          start_ang=SPRING_ANGLE)
    caption(d, 'between stations it blends from one to the other')
    return img


def clip_blend():
    img, d = canvas()
    text(d, (W / 2, 18), 'the same four pitches, joined two ways', F_LABEL, INK, 'mm')
    for i, (name, smooth) in enumerate((('Linear', False), ('Smooth', True))):
        x0 = 20 + i * 140
        pitch_chart(d, (x0, 42, x0 + 120, 128), SPRING, smooth, True)
        text(d, (x0 + 60, 142), name, F_LABEL, INK if smooth else MUTED, 'mm')
    caption(d, 'a different shape, so a different height')
    return img


def clip_ends():
    img, d = canvas()
    text(d, (W / 2, 18), 'how the helix finishes at each end', F_LABEL, INK, 'mm')
    helix(d, PLAIN, (26, 34, 142, 156), True, marks=(0.0,))
    text(d, (84, 168), 'Natural', F_LABEL, MUTED, 'mm')
    helix(d, FLAT_END, (158, 34, 274, 156), True, lit=(0.0, 1.2), marks=(1.2,))
    text(d, (216, 168), 'Flat', F_LABEL, INK, 'mm')
    caption(d, 'a flat end adds coils before the first station')
    return img


def clip_radius():
    img, d = canvas()
    text(d, (W / 2, 20), 'radius is set at each station too', F_LABEL, INK, 'mm')
    helix(d, CONE, BOX, True, marks=(0.0, 3.0), numbered=True)
    caption(d, 'two different radii taper the helix between them')
    return img


def clip_stations():
    img, d = canvas()
    text(d, (W / 2, 20), 'a station is one point along the helix', F_LABEL, INK, 'mm')
    helix(d, SPRING, BOX, True, marks=SPRING_MARKS, numbered=True,
          start_ang=SPRING_ANGLE)
    caption(d, 'each one carries a pitch and a radius')
    return img


CLIPS = (('pitch-rise.png', clip_pitch), ('turns.png', clip_turns),
         ('pitch.png', clip_pitch_cell), ('blend.png', clip_blend),
         ('ends.png', clip_ends), ('radius.png', clip_radius),
         ('stations.png', clip_stations))

if __name__ == '__main__':
    for name, fn in CLIPS:
        save(fn(), name)
