"""Helix3D - parametric 3D helix sketch curve for Fusion.

Two ways to use it:

* Outside a sketch: creates a custom feature ("Helix") wrapping a 3D sketch that
  holds one fixed spline. Radius, pitch/height/turns (depending on mode), taper
  and start angle are model parameters visible in the Parameters dialog.
  Right-click > Edit Feature reopens the dialog.

* Inside a sketch: adds the helix straight into the active sketch as a fixed
  spline, with its definition stored as attributes on the curve. Select the
  curve, right-click > Edit 3D Helix to change it. Expressions referencing user
  parameters are stored on the curve and re-evaluated after every command, so
  the curve follows its parameters while the add-in is running. It is not a
  custom feature (one can't wrap the sketch you're editing without swallowing
  it), so the values don't appear as rows in the Parameters dialog.

The curve is a degree-3 non-rational B-spline interpolating sampled helix
points with exact end tangents. No solids or surfaces are involved.
"""
import adsk.core
import adsk.fusion
import json
import math
import os
import re
import shutil
import stat
import sys
import threading
import time
import traceback
import zipfile

app = adsk.core.Application.get()
ui = app.userInterface

CF_ID = 'moyseHelix3D_v2'
CMD_ID = 'scottHelix3DCreate'
EDIT_ID = 'scottHelix3DEdit'
SKETCH_EDIT_ID = 'scottHelix3DSketchEdit'
VAR_CF_ID = 'moyseHelix3DVar'
VAR_CMD_ID = 'scottHelix3DVarCreate'
VAR_EDIT_ID = 'scottHelix3DVarEdit'
OUR_COMMANDS = (CMD_ID, EDIT_ID, SKETCH_EDIT_ID, VAR_CMD_ID, VAR_EDIT_ID)
PANELS = ('SketchCreatePanel', 'SolidCreatePanel')
ICONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources')
VAR_ICONS = os.path.join(ICONS, 'variable')   # same helix, uneven coils
TOOLCLIPS = os.path.join(ICONS, 'toolclips')  # pictures Fusion shows inside a tooltip
ATTR_GROUP = 'Helix3D'
SAMPLES_PER_TURN = 24
PREVIEW_SAMPLES_PER_TURN = 12   # previews only have to look right; see build_curve

MODES = ['Revolutions & Pitch', 'Revolutions & Height', 'Height & Pitch', 'Spiral (flat)',
         'Path & Pitch', 'Path & Revolutions']
MODE_RP, MODE_RH, MODE_HP, MODE_SPIRAL, MODE_PP, MODE_PR = MODES
PATH_MODES = (MODE_PP, MODE_PR)
# The variable pitch helix has its own command and its own dialog, so this is a
# spec value rather than another item in the Mode dropdown.
MODE_VAR = 'Variable Pitch'
BLENDS = [('Smooth', 'smooth'), ('Linear', 'linear')]
END_TYPES = [('Natural', 'natural'), ('Flat', 'flat')]   # Inventor's words for coil ends
END_FIELDS = ('Pitch', 'Flat', 'Blend')                  # startPitch, startFlat, startBlend ...
MAX_STATIONS = 10
PICKERS = ('plane', 'path', 'center', 'start')   # dialog order; selection focus walks along it


class HelixError(Exception):
    """A message for the user, not a traceback."""

_handlers = []
_def = None
_def_var = None
_editing = None         # CustomFeature in the feature edit dialog
_editing_xf = None      # its sketch's transform (sketch -> model), read before rolling back
_editing_restore = None # TimelineObject the marker sat after when the edit dialog opened
_editing_rolled = False
_editing_activated = False  # activate re-fires after every pan/orbit; only the first one sets up
_editing_deps = {}      # {'center': entity, 'start': entity} as stored when the dialog opened
_touched = set()        # point inputs the user changed (or cleared) in the edit dialog
_touch_armed = False    # ignore inputChanged until the dialog has finished setting itself up
TOUCH_EVT = 'scottHelix3DDeferredTouch'
VAR_POLL_EVT = 'scottHelix3DVarPoll'
_touch_pending = set()  # entity tokens of features rebuilt by compute since the last touch
_touching = False
_editing_curve = None   # SketchFixedSpline in the sketch edit dialog
_origin_restore = None  # (component, previous lightbulb state)
_pending_triad = None   # Matrix3D to push onto the triad when the dialog activates
_pending_selections = []  # [(input id, entity)] to select once the dialog activates
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Helix3D.log')
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Helix3D.settings.json')
_triad_last = None      # last meaningful triad transform (world); restored when the triad is re-shown
_seeded_end_radius = False  # end radius pre-filled from the radius once per dialog
_var_focus = None       # ('row', k), ('start',) or ('end',): the part of the helix the dialog is on
_var_cmd = None         # the open variable pitch Command, watched for row clicks
_var_last_row = -2      # table.selectedRow as last seen by the watcher
_var_poll_stop = None   # threading.Event that ends the watcher


def _load_settings():
    try:
        with open(SETTINGS_FILE, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_settings():
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(_settings, f)
    except OSError:
        pass


_settings = _load_settings()


def _addin_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _log(msg):
    app.log(msg)
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(msg + '\n')
    except OSError:
        pass


# --------------------------------------------------------------------------
# Geometry: helix/spiral -> interpolating cubic B-spline
# --------------------------------------------------------------------------
def _basis_funs(i, u, p, U):
    N = [0.0] * (p + 1)
    left = [0.0] * (p + 1)
    right = [0.0] * (p + 1)
    N[0] = 1.0
    for j in range(1, p + 1):
        left[j] = u - U[i + 1 - j]
        right[j] = U[i + j] - u
        saved = 0.0
        for r in range(j):
            temp = N[r] / (right[r + 1] + left[j - r])
            N[r] = saved + right[r + 1] * temp
            saved = left[j - r] * temp
        N[j] = saved
    return N


def _find_span(n, p, u, U):
    if u >= U[n + 1]:
        return n
    lo, hi = p, n + 1
    mid = (lo + hi) // 2
    while u < U[mid] or u >= U[mid + 1]:
        if u < U[mid]:
            hi = mid
        else:
            lo = mid
        mid = (lo + hi) // 2
    return mid


def _sample_count(turns, per_turn):
    return max(6, int(math.ceil(abs(turns) * per_turn)))


def helix_nurbs(r0, dr, height, turns, start_ang, right_hand, per_turn=SAMPLES_PER_TURN):
    """Control points and knots for a cubic B-spline helix around Z, centred on
    the origin, starting at z=0. Radius varies linearly from r0 to r0+dr."""
    n = _sample_count(turns, per_turn)
    total_ang = 2 * math.pi * turns * (1 if right_hand else -1)

    def pt(t):
        a = start_ang + total_ang * t
        r = r0 + dr * t
        return (r * math.cos(a), r * math.sin(a), height * t)

    def deriv(t):
        a = start_ang + total_ang * t
        r = r0 + dr * t
        return (dr * math.cos(a) - r * math.sin(a) * total_ang,
                dr * math.sin(a) + r * math.cos(a) * total_ang,
                height)

    return _fit_nurbs([pt(k / n) for k in range(n + 1)], deriv(0.0), deriv(1.0))


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _scale(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _unit(a):
    n = math.sqrt(_dot(a, a))
    return _scale(a, 1.0 / n) if n > 1e-12 else (0.0, 0.0, 0.0)


def _rmf(samples, up):
    """Rotation-minimising frames (Wang et al. double reflection) along sampled
    (point, tangent) pairs. Returns (point, t, r, b) per sample with r the
    reference direction, starting sideways from the path within the plane
    normal to `up`."""
    pts = [s[0] for s in samples]
    tans = [_unit(s[1]) for s in samples]
    r = _unit(_cross(tans[0], up))
    if _dot(r, r) < 0.5:
        for axis in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)):
            r = _unit(_cross(tans[0], axis))
            if _dot(r, r) > 0.5:
                break
    frames = []
    for i, (p, t) in enumerate(zip(pts, tans)):
        if i:
            v1 = _sub(p, pts[i - 1])
            c1 = _dot(v1, v1)
            if c1 > 1e-18:
                rL = _sub(r, _scale(v1, 2 * _dot(v1, r) / c1))
                tL = _sub(tans[i - 1], _scale(v1, 2 * _dot(v1, tans[i - 1]) / c1))
                v2 = _sub(t, tL)
                c2 = _dot(v2, v2)
                r = rL if c2 < 1e-18 else _sub(rL, _scale(v2, 2 * _dot(v2, rL) / c2))
            r = _unit(_sub(r, _scale(t, _dot(r, t))))
        frames.append((p, t, r, _cross(t, r)))
    return frames


def path_helix_nurbs(sampler, r0, dr, turns, start_ang, right_hand, up,
                     per_turn=SAMPLES_PER_TURN, flip=False):
    """Helix wound around a path. sampler(ts) -> [(point, tangent), ...] at
    those fractions of the path's arc length, in the sampler's own space.
    flip walks the path from the far end instead."""
    n = _sample_count(turns, per_turn)
    d = 0.1 / n   # two extra samples at each end for second-order end tangents
    ts = [0.0, d, 2 * d] + [k / n for k in range(1, n)] + [1.0 - 2 * d, 1.0 - d, 1.0]
    total_ang = 2 * math.pi * turns * (1 if right_hand else -1)
    samples = sampler([1.0 - t for t in ts] if flip else ts)
    if flip:
        # Tangents come back along increasing parameter, so they point behind
        # us now. Turning them round keeps the winding the same hand.
        samples = [(p, (-vx, -vy, -vz)) for p, (vx, vy, vz) in samples]
    Q = []
    for t, (p, tv, r, b) in zip(ts, _rmf(samples, up)):
        a = start_ang + total_ang * t
        rad = r0 + dr * t
        Q.append(tuple(p[c] + rad * (math.cos(a) * r[c] + math.sin(a) * b[c]) for c in range(3)))

    def one_sided(q0, q1, q2, h):   # f'(0) ~ (-3 f0 + 4 f1 - f2) / 2h
        return tuple((-3 * q0[c] + 4 * q1[c] - q2[c]) / (2 * h) for c in range(3))
    D0 = one_sided(Q[0], Q[1], Q[2], d)
    Dn = one_sided(Q[-1], Q[-2], Q[-3], -d)
    return _fit_nurbs([Q[0]] + Q[3:-3] + [Q[-1]], D0, Dn)


def _fit_nurbs(Q, D0, Dn):
    """Cubic B-spline through the points Q (uniformly parameterised) with end
    tangents D0, Dn (Piegl & Tiller 9.2.1)."""
    n = len(Q) - 1
    uk = [k / n for k in range(n + 1)]
    p, m = 3, n + 3
    U = [0.0] * 4 + uk[1:n] + [1.0] * 4
    P = [None] * m
    P[0], P[m - 1] = Q[0], Q[n]
    P[1] = tuple(Q[0][c] + U[4] / 3.0 * D0[c] for c in range(3))
    P[m - 2] = tuple(Q[n][c] - (1 - U[m - 1]) / 3.0 * Dn[c] for c in range(3))

    # Only four basis functions are non-zero at any u, so each row of the
    # interpolation matrix has at most four entries, all close to the diagonal.
    # Storing and solving just that band keeps a 24 turn helix quick; solving it
    # as a dense matrix costs the square of the sample count.
    nu = n - 1
    rows = []
    rhs = [[0.0] * 3 for _ in range(nu)]
    half = 0
    for r in range(nu):
        u = uk[r + 1]
        span = _find_span(m - 1, p, u, U)
        N = _basis_funs(span, u, p, U)
        cols = {}
        for j in range(p + 1):
            idx = span - p + j
            if idx <= 1 or idx >= m - 2:
                for c in range(3):
                    rhs[r][c] -= N[j] * P[idx][c]
            else:
                cols[idx - 2] = cols.get(idx - 2, 0.0) + N[j]
        for c in range(3):
            rhs[r][c] += Q[r + 1][c]
        rows.append(cols)
        for col in cols:
            half = max(half, abs(col - r))

    band = [[0.0] * (2 * half + 1) for _ in range(nu)]
    for r, cols in enumerate(rows):
        for col, value in cols.items():
            band[r][col - r + half] = value

    # No row swapping: this matrix is diagonally dominant, and swapping rows
    # would spread entries outside the band.
    for i in range(nu):
        piv = band[i][half]
        top = min(i + half, nu - 1)
        for k in range(i + 1, top + 1):
            f = band[k][half - (k - i)]
            if f == 0.0:
                continue
            f /= piv
            for col in range(i, top + 1):
                band[k][col - k + half] -= f * band[i][col - i + half]
            for c in range(3):
                rhs[k][c] -= f * rhs[i][c]

    X = [[0.0] * 3 for _ in range(nu)]
    for i in range(nu - 1, -1, -1):
        top = min(i + half, nu - 1)
        d = band[i][half]
        for c in range(3):
            s = rhs[i][c]
            for j in range(i + 1, top + 1):
                s -= band[i][j - i + half] * X[j][c]
            X[i][c] = s / d
    for i in range(nu):
        P[i + 2] = tuple(X[i])
    return P, U


def _pchip_end_slope(d0, d1, h0, h1):
    """Slope at the first or last station: the one sided estimate, clamped so
    the end span cannot overshoot (Fritsch-Carlson)."""
    m = ((2.0 * h0 + h1) * d0 - h0 * d1) / (h0 + h1)
    if m * d0 <= 0.0:
        return 0.0
    if d0 * d1 <= 0.0 and abs(m) > abs(3.0 * d0):
        return 3.0 * d0
    return m


def _pchip_slopes(xs, ys):
    """Monotone cubic slopes (Fritsch-Carlson). Two stations at the same value
    give an exactly flat run between them, which is what a dwell and a stretch
    of constant pitch both need, and no span ever overshoots its two ends."""
    n = len(xs)
    h = [xs[i + 1] - xs[i] for i in range(n - 1)]
    d = [(ys[i + 1] - ys[i]) / h[i] for i in range(n - 1)]
    if n == 2:
        return [d[0], d[0]]
    m = [0.0] * n
    for i in range(1, n - 1):
        if d[i - 1] * d[i] <= 0.0:
            continue            # a turning point, so flat here: no overshoot
        w1, w2 = 2.0 * h[i] + h[i - 1], h[i] + 2.0 * h[i - 1]
        m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i])
    m[0] = _pchip_end_slope(d[0], d[1], h[0], h[1])
    m[-1] = _pchip_end_slope(d[-1], d[-2], h[-1], h[-2])
    return m


class _Ramp:
    """A quantity that varies along the helix: given at the stations, cubic in
    between. It carries its own integral, because the height of a variable
    pitch helix is the area under the pitch. smooth=False ramps in straight
    lines instead, the way SolidWorks does it."""

    def __init__(self, xs, ys, smooth=True):
        self.xs, self.ys, self.smooth = xs, ys, smooth
        self.h = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
        self.m = _pchip_slopes(xs, ys) if smooth else None
        self.cum = [0.0]
        for i in range(len(self.h)):
            self.cum.append(self.cum[-1] + self._area(i, 1.0))

    def _span(self, x):
        i = 0
        while i < len(self.h) - 1 and x >= self.xs[i + 1]:
            i += 1
        return i, min(max((x - self.xs[i]) / self.h[i], 0.0), 1.0)

    def _ends(self, i):
        y0, y1 = self.ys[i], self.ys[i + 1]
        if self.smooth:
            return y0, y1, self.m[i], self.m[i + 1]
        s = (y1 - y0) / self.h[i]
        return y0, y1, s, s

    def value(self, x):
        i, t = self._span(x)
        y0, y1, m0, m1 = self._ends(i)
        t2, t3 = t * t, t * t * t
        return (y0 * (2 * t3 - 3 * t2 + 1) + y1 * (3 * t2 - 2 * t3)
                + self.h[i] * (m0 * (t3 - 2 * t2 + t) + m1 * (t3 - t2)))

    def slope(self, x):
        i, t = self._span(x)
        y0, y1, m0, m1 = self._ends(i)
        t2 = t * t
        return ((6 * t2 - 6 * t) * (y0 - y1) / self.h[i]
                + m0 * (3 * t2 - 4 * t + 1) + m1 * (3 * t2 - 2 * t))

    def _area(self, i, t):
        """Integral over the first t of span i. Exact: the span is a cubic."""
        y0, y1, m0, m1 = self._ends(i)
        h = self.h[i]
        t2, t3, t4 = t * t, t * t * t, t * t * t * t
        return (h * (y0 * (t4 / 2 - t3 + t) + y1 * (t3 - t4 / 2))
                + h * h * (m0 * (t4 / 4 - 2 * t3 / 3 + t2 / 2) + m1 * (t4 / 4 - t3 / 3)))

    def integral(self, x):
        i, t = self._span(x)
        return self.cum[i] + self._area(i, t)

    def total(self):
        return self.cum[-1]


def _station_axes(stations):
    """(turns at each station, pitches, radii), with the values checked."""
    if len(stations) < 2:
        raise HelixError('A variable pitch helix needs at least two stations.')
    xs, ps, rs = [0.0], [], []
    for i, st in enumerate(stations):
        p, r = st.get('pitch', 0.0), st.get('radius', 0.0)
        if p < -1e-9:
            raise HelixError('Station %d: pitch cannot be negative. Use Flip direction '
                             'to run the helix the other way.' % (i + 1))
        if r <= 1e-9:
            raise HelixError('Station %d: radius has to be more than zero.' % (i + 1))
        ps.append(max(p, 0.0))
        rs.append(r)
        if i < len(stations) - 1:
            t = st.get('turns', 0.0)
            if t <= 1e-9:
                raise HelixError('Station %d: the turns to the next station have to be '
                                 'more than zero.' % (i + 1))
            xs.append(xs[-1] + t)
    return xs, ps, rs


def var_helix_height(stations, smooth=True):
    xs, ps, _ = _station_axes(stations)
    return _Ramp(xs, ps, smooth).total()


def var_helix_nurbs(stations, start_ang, right_hand, smooth=True, flip=False,
                    per_turn=SAMPLES_PER_TURN, u_range=None):
    """Control points and knots for a helix around Z whose pitch and radius
    follow the stations. Pitch is rise per turn, so the height is the area under
    the pitch. The pitch is smooth in slope, which makes the height smooth in
    curvature, and that is what stops a sweep creasing at a station.
    u_range=(from, to) in turns builds just that stretch, for the preview
    highlight."""
    xs, ps, rs = _station_axes(stations)
    pitch, radius = _Ramp(xs, ps, smooth), _Ramp(xs, rs, smooth)
    sign = -1.0 if flip else 1.0
    rate = 2 * math.pi * (1 if right_hand else -1) * sign

    def pt(u):
        a = start_ang + rate * u
        r = radius.value(u)
        return (r * math.cos(a), r * math.sin(a), sign * pitch.integral(u))

    def deriv(u):
        a = start_ang + rate * u
        r, dr = radius.value(u), radius.slope(u)
        return (dr * math.cos(a) - r * math.sin(a) * rate,
                dr * math.sin(a) + r * math.cos(a) * rate,
                sign * pitch.value(u))

    if u_range is not None:
        u0, u1 = max(u_range[0], 0.0), min(u_range[1], xs[-1])
        k = _sample_count(u1 - u0, per_turn)
        us = [u0 + (u1 - u0) * j / k for j in range(k + 1)]
    else:
        # Sample each span on its own so a sample lands exactly on every station.
        us = [0.0]
        for i in range(len(xs) - 1):
            k = _sample_count(xs[i + 1] - xs[i], per_turn)
            us.extend(xs[i] + (xs[i + 1] - xs[i]) * j / k for j in range(1, k + 1))
    # The fit runs on 0..1, so the end tangents have to be per unit of that, not
    # per turn. Getting this wrong costs three hundred times the radial error.
    span = us[-1] - us[0]
    D0, Dn = deriv(us[0]), deriv(us[-1])
    return _fit_nurbs([pt(u) for u in us],
                      tuple(c * span for c in D0), tuple(c * span for c in Dn))


def resolve(spec):
    """Turn a spec dict (mode + raw values, internal units) into
    (r0, dr, height, turns). Values not used by the mode are ignored."""
    mode = spec['mode']
    r0 = spec['radius']
    if mode == MODE_RP:
        turns, pitch = spec['turns'], spec['pitch']
        height = pitch * turns
    elif mode == MODE_RH:
        turns, height = spec['turns'], spec['height']
    elif mode == MODE_HP:
        height, pitch = spec['height'], spec['pitch']
        turns = height / pitch if pitch else 0.0
    elif mode in PATH_MODES:
        height = spec.get('_length', 0.0)   # the path's arc length
        turns = (height / spec['pitch'] if spec['pitch'] else 0.0) if mode == MODE_PP else spec['turns']
    else:  # spiral
        turns, height = spec['turns'], 0.0
    if mode == MODE_SPIRAL or spec.get('taperBy', 'angle') == 'radius':
        dr = spec['endRadius'] - r0
    else:
        dr = math.tan(spec.get('taper', 0.0)) * height
    return r0, dr, height, turns


def _curve_from(P, U, xf):
    pts = [adsk.core.Point3D.create(*p) for p in P]
    if xf:
        M = adsk.core.Matrix3D.create()
        M.setWithArray(xf)
        for pt in pts:
            pt.transformBy(M)
    return adsk.core.NurbsCurve3D.createNonRational(pts, 3, U, False)


def build_curve(spec, per_turn=SAMPLES_PER_TURN):
    """NurbsCurve3D in sketch space from a spec dict. per_turn is dropped for
    previews, where the curve only has to look right; everything that gets
    committed is built at the full sample rate."""
    # A flat spiral has no axis to run along, so flipping it would only change
    # which way it winds, which is what Direction is for.
    flip = bool(spec.get('flip')) and spec['mode'] != MODE_SPIRAL
    if spec['mode'] in PATH_MODES:
        if not spec.get('_sampler'):
            raise HelixError('Pick a path for the helix to follow.')
        r0, dr, height, turns = resolve(spec)
        P, U = path_helix_nurbs(spec['_sampler'], r0, dr, turns, spec['startAngle'],
                                spec['hand'] == 'right', spec['_up'], per_turn, flip)
        pts = [adsk.core.Point3D.create(*p) for p in P]
        for pt in pts:
            pt.transformBy(spec['_Si'])   # path is sampled in model space
        return adsk.core.NurbsCurve3D.createNonRational(pts, 3, U, False)
    if spec['mode'] == MODE_VAR:
        P, U = var_helix_nurbs(_expanded_stations(spec), spec['startAngle'], spec['hand'] == 'right',
                               spec.get('blend', 'smooth') == 'smooth', flip, per_turn)
        return _curve_from(P, U, spec.get('xform'))
    r0, dr, height, turns = resolve(spec)
    if flip:
        # Reverse the climb and the sweep together. Reversing only the climb
        # would turn a right hand helix into a left hand one.
        height, turns = -height, -turns
    P, U = helix_nurbs(r0, dr, height, turns, spec['startAngle'], spec['hand'] == 'right', per_turn)
    return _curve_from(P, U, spec.get('xform'))


def _path_geometry(ent):
    sc = adsk.fusion.SketchCurve.cast(ent)
    if sc:
        return sc.worldGeometry
    ed = adsk.fusion.BRepEdge.cast(ent)
    if ed:
        return ed.geometry
    return None


def _path_sampler(curve3d):
    """(sampler, length): sampler(ts) gives [(point, tangent), ...] at those
    fractions of the arc length, in the curve's own (model) space. Points and
    derivatives come back one call each, because a round trip into Fusion per
    sample is what makes a long path slow to preview."""
    ev = curve3d.evaluator
    _, pmin, pmax = ev.getParameterExtents()
    _, length = ev.getLengthAtParameter(pmin, pmax)

    def sample(ts):
        params = []
        for t in ts:
            _, u = ev.getParameterAtLength(pmin, min(max(t, 0.0), 1.0) * length)
            params.append(u)
        got_points, points = ev.getPointsAtParameters(params)
        got_derivs, derivs = ev.getFirstDerivatives(params)
        if not (got_points and got_derivs):
            raise HelixError('Could not evaluate that path curve.')
        return [((p.x, p.y, p.z), (d.x, d.y, d.z)) for p, d in zip(points, derivs)]
    return sample, length


def _attach_path(spec, sketch_xf, path_ent):
    """For the path modes: hang the sampler, the path length, the model->sketch
    matrix and the sketch normal (angle reference) on the spec."""
    if spec['mode'] not in PATH_MODES:
        return
    curve = _path_geometry(path_ent) if path_ent is not None and path_ent.isValid else None
    if curve is None:
        spec['_sampler'] = None
        return
    spec['_sampler'], spec['_length'] = _path_sampler(curve)
    spec['_Si'] = _model_to_sketch(sketch_xf)
    _, _, _, z = sketch_xf.getAsCoordinateSystem()
    spec['_up'] = (z.x, z.y, z.z)


# --------------------------------------------------------------------------
# Dialog
# --------------------------------------------------------------------------
def _in_sketch():
    return adsk.fusion.Sketch.cast(app.activeEditObject)


def _triad_to_sketch(sketch, W):
    """Helix points are built in triad-local space (axis = local Z). Map them
    into sketch space: M = S^-1 * W."""
    Si = sketch.transform.copy()
    Si.invert()
    M = W.copy()
    M.transformBy(Si)      # pre-multiply -> S^-1 * W
    return M


def _add_inputs(inputs, spec=None, context='create', sketch=None):
    """context: 'create' | 'edit_feature' | 'edit_sketch'; sketch: the sketch an
    in-sketch helix lives in (enables the placement triad)."""
    global _pending_selections, _seeded_end_radius
    _pending_selections = []
    lu = app.activeProduct.unitsManager.defaultLengthUnits
    spec = spec or {}
    _seeded_end_radius = 'endRadius' in spec   # an existing end radius must not be overwritten
    v = lambda k, d: _seed(spec, k, d)

    dd = inputs.addDropDownCommandInput('mode', 'Mode', adsk.core.DropDownStyles.TextListDropDownStyle)
    cur = spec.get('mode', MODE_RP)
    for mname in MODES:
        dd.listItems.add(mname, mname == cur)
    dd.isEnabled = context != 'edit_feature'  # can't add/remove params on an existing feature

    _add_pickers(inputs, spec, context)
    if context == 'create' and _in_sketch():
        inputs.addBoolValueInput('asFeature', 'Finish sketch and create parametric feature', True, '', False)

    inputs.addValueInput('radius', 'Radius', lu, v('radius', 2.0))
    inputs.addValueInput('endRadius', 'End Radius', lu, v('endRadius', 4.0))
    inputs.addValueInput('pitch', 'Pitch', lu, v('pitch', 1.0))
    inputs.addValueInput('height', 'Height', lu, v('height', 3.0))
    inputs.addValueInput('turns', 'Revolutions', '', v('turns', 3.0))
    tb = inputs.addDropDownCommandInput('taperBy', 'Taper', adsk.core.DropDownStyles.TextListDropDownStyle)
    cur_tb = spec.get('taperBy', 'angle')
    for label, key in TAPER_BY:
        tb.listItems.add(label, key == cur_tb)
    tb.isEnabled = context != 'edit_feature'   # decides which parameter the feature owns
    inputs.addValueInput('taper', 'Taper Angle', 'deg', v('taper', 0.0))
    inputs.addValueInput('startAngle', 'Start Angle', 'deg', v('startAngle', 0.0))
    hd = inputs.addDropDownCommandInput('hand', 'Direction', adsk.core.DropDownStyles.TextListDropDownStyle)
    rh = spec.get('hand', 'right') == 'right'
    hd.listItems.add('Right hand', rh)
    hd.listItems.add('Left hand', not rh)
    inputs.addBoolValueInput('flip', 'Flip direction', True, '', bool(spec.get('flip')))

    _add_placement(inputs, spec, sketch)
    _add_const_tooltips(inputs)
    _apply_mode_visibility(inputs)


def _add_const_tooltips(inputs):
    """Hover text for the constant pitch dialog. Set here rather than where the
    inputs are built, so all the wording sits in one place and the pickers and
    the placement triad, which are shared with the other command, get wording
    that suits this one."""
    _tip(inputs, 'mode', 'Which two numbers you want to give it',
         'Every mode builds the same kind of curve. What changes is which two values you '
         'type and which one Fusion works out for you.<br><br>It greys out when you edit a '
         'finished feature, because Fusion fixes which parameters a feature owns at the '
         'moment it is created.')
    _tip(inputs, 'path', 'The curve the helix winds around',
         'Pick a sketch curve or the edge of a body and the helix wraps round it instead of '
         'round a straight axis. Pitch is measured along the curve, so the coils stay evenly '
         'spaced round a bend.<br><br>One curve at a time. A path made of several joined '
         'segments will not work yet.', 'path.png')
    _tip(inputs, 'start', 'A point for the helix to begin at',
         'It sets the radius, the angle it starts at and how far along the axis it begins, '
         'so those boxes disappear while it is set.')
    _tip(inputs, 'radius', 'How far the curve sits from the axis',
         'On a tapered helix this is the radius at the start. The far end comes from the '
         'taper angle, or from the end radius.')
    _tip(inputs, 'endRadius', 'The radius at the far end',
         'Use this when you know the size you want to finish on and would rather not work '
         'the angle out. On a flat spiral it is the outside radius.', 'taper.png')
    _tip(inputs, 'pitch', 'How far the helix climbs in one turn',
         'It holds the same value the whole way along. Wound on a path it is measured along '
         'the path, so the coils stay evenly spaced round a bend.<br><br>For a pitch that '
         'changes as it goes, use the Variable Pitch Helix command instead.')
    _tip(inputs, 'height', 'How far the helix climbs in total',
         'Measured along the axis, from where the curve starts to where it finishes.')
    _tip(inputs, 'turns', 'How many times it goes round',
         'It does not have to be a whole number. Two and a half finishes on the opposite '
         'side from where it started.')
    _tip(inputs, 'taperBy', 'Whether you give the taper an angle or a finish radius',
         'Both reach the same shape. Pick whichever number you already have.<br><br>It greys '
         'out when you edit a finished feature, because it decides which parameter the '
         'feature owns.', 'taper.png')
    _tip(inputs, 'taper', 'How much the radius opens out as it climbs',
         'Zero keeps the helix straight up. A positive angle opens it out as it rises and a '
         'negative one closes it in, like a conical spring.', 'taper.png')
    _tip(inputs, 'flip', 'Run the helix the other way',
         'Down the axis instead of up, or from the far end of the path back towards the '
         'start. It stays right or left handed either way, so on a path helix with no taper '
         'there is nothing to see. Put a taper on it and the wide end swaps ends.')
    _add_shared_tooltips(inputs)


def _add_shared_tooltips(inputs):
    """The pickers and the placement triad, which both dialogs build the same
    way and which mean the same thing in each."""
    _tip(inputs, 'plane', 'The flat plane the helix is built on',
         'The axis points straight out of this plane. Leave it empty and Fusion uses the XY '
         'plane, or the plane the centre point sits on if you pick one.')
    _tip(inputs, 'center', 'A point for the axis to pass through',
         'Use it to put the helix somewhere other than the origin. Leave it empty and the '
         'axis sits on the origin of the plane.')
    _tip(inputs, 'placement_grp', 'Position and direction when no points are picked',
         'Drag the straight arrows to slide the helix along an axis and the arcs to spin it. '
         'It only shows when a centre point and a start point have not already pinned it.')
    _tip(inputs, 'axis', 'Which way the helix axis points',
         'The coils lie in the plane you choose here, and the helix winds along the axis at '
         'right angles to it.')
    _tip(inputs, 'startAngle', 'Where around the circle the helix begins',
         'Zero starts it on the X axis of the plane. It hides when you pick a start point, '
         'because the point decides the angle instead.')
    _tip(inputs, 'hand', 'Which way the helix winds as it climbs',
         'Right hand is the usual one, the same as an ordinary screw thread. Left hand winds '
         'the other way.')
    _tip(inputs, 'asFeature', 'Close this sketch and wrap the helix as a feature',
         'Leave it off and the curve goes into the sketch you are editing. Turn it on and '
         'Fusion closes the sketch and makes the helix a feature in the timeline, which puts '
         'its values in the Parameters dialog where other parameters can drive them.')
    _tip(inputs, 'placement', 'Drag to move and turn the helix')


def _seed(spec, key, default):
    """Seed a value box from the expression that was typed if there is one, so
    reopening the dialog and pressing OK does not flatten `wire * 3` into a
    number. Falls back to the value if the expression no longer evaluates."""
    e = (spec.get('expr') or {}).get(key)
    if e:
        try:
            app.activeProduct.unitsManager.evaluateExpression(e, _units()[_unit_of(key)])
            return adsk.core.ValueInput.createByString(e)
        except Exception:
            pass
    return adsk.core.ValueInput.createByReal(spec.get(key, default))


def _add_pickers(inputs, spec, context, with_path=True, start_tip=None):
    """The plane, path, centre and start selection inputs, shared by both
    dialogs. Missing ones are simply not added; everything guards on that."""
    if context == 'create' and not _in_sketch():
        sel = inputs.addSelectionInput('plane', 'Plane', 'Sketch plane. Helix axis is the plane normal. Empty = XY, or the centre point\'s sketch plane.')
        sel.addSelectionFilter('ConstructionPlanes')
        sel.addSelectionFilter('PlanarFaces')
        sel.setSelectionLimits(0, 1)
    points = ('SketchPoints', 'ConstructionPoints', 'Vertices')
    rows = [('center', 'Center Point', 'Optional. The helix axis passes through this point.', points),
            ('start', 'Start Point',
             start_tip or 'Optional. Where the helix starts: sets radius, start angle and axial offset.',
             points)]
    if with_path:
        rows.insert(0, ('path', 'Path',
                        'Curve or edge the helix winds around. Pitch is measured along it.',
                        ('SketchCurves', 'Edges')))
    for sid, label, tip, filters in rows:
        sel = inputs.addSelectionInput(sid, label, tip)
        for flt in filters:
            sel.addSelectionFilter(flt)
        sel.setSelectionLimits(0, 1)
        ent = spec.get('_deps', {}).get(sid)
        if ent:
            # Selections can't be set during commandCreated; do it in activate.
            _pending_selections.append((sid, ent))


def _add_placement(inputs, spec, sketch):
    """In-sketch create or edit: the placement triad and its base plane."""
    if sketch is None:
        return
    grp = inputs.addGroupCommandInput('placement_grp', 'Placement')
    grp.isExpanded = _settings.get('placementExpanded', True)
    grp.tooltip = 'Position and axis of the helix when it is not pinned by a centre and start point.'
    ax = grp.children.addDropDownCommandInput('axis', 'Base plane', adsk.core.DropDownStyles.TextListDropDownStyle)
    cur_axis = spec.get('axis', 'Z')
    for label, key in AXIS_CHOICES:
        ax.listItems.add(label, key == cur_axis)
    M = sketch.transform.copy()      # default: sketch origin, axis = sketch normal
    if spec.get('triad'):
        M.setWithArray(spec['triad'])  # raw triad transform, world space
    triad = grp.children.addTriadCommandInput('placement', M)
    triad.hideAllScaling()
    triad.setFlipVisibility(False)
    triad.transform = M          # the constructor argument is not reliably honoured
    global _pending_triad, _triad_last
    _pending_triad = M           # re-applied once the dialog is showing (activate)
    _triad_last = M


def _apply_mode_visibility(inputs):
    mode = adsk.core.DropDownCommandInput.cast(inputs.itemById('mode')).selectedItem.name
    path_mode = mode in PATH_MODES
    show = {
        MODE_RP: ('pitch', 'turns', 'taper'),
        MODE_RH: ('turns', 'height', 'taper'),
        MODE_HP: ('height', 'pitch', 'taper'),
        MODE_SPIRAL: ('endRadius', 'turns'),
        MODE_PP: ('pitch', 'taper'),
        MODE_PR: ('turns', 'taper'),
    }[mode]
    taper_by = _taper_by(inputs)
    if 'taper' in show and taper_by == 'radius':
        show = tuple('endRadius' if k == 'taper' else k for k in show)
    for k in ('endRadius', 'pitch', 'height', 'turns', 'taper'):
        inputs.itemById(k).isVisible = k in show
    inputs.itemById('taperBy').isVisible = mode != MODE_SPIRAL
    inputs.itemById('flip').isVisible = mode != MODE_SPIRAL
    # The plane only matters as the sketch the curve lives in and the reference
    # for the zero start angle; along a path both default to the path's sketch.
    for sid, vis in (('plane', not path_mode), ('path', path_mode), ('center', not path_mode), ('start', not path_mode)):
        sel = adsk.core.SelectionCommandInput.cast(inputs.itemById(sid))
        if sel:
            if not vis and sel.selectionCount:
                sel.clearSelection()
            sel.isVisible = vis
    has_start = not path_mode and _effective_point(inputs, 'start') is not None
    has_center = not path_mode and _effective_point(inputs, 'center') is not None
    inputs.itemById('radius').isVisible = not has_start
    inputs.itemById('startAngle').isVisible = not has_start
    # Both points pin the helix (axis = sketch normal); a path places it entirely.
    _apply_placement_visibility(inputs, not (path_mode or (has_center and has_start)))


def _apply_placement_visibility(inputs, want):
    grp = _find(inputs, 'placement_grp')
    if grp and grp.isVisible != want:
        grp.isVisible = want
        tri = adsk.core.TriadCommandInput.cast(_find(inputs, 'placement'))
        if want and tri and _triad_last:
            tri.transform = _triad_last   # showing the triad again resets it


def _taper_by(inputs):
    name = adsk.core.DropDownCommandInput.cast(inputs.itemById('taperBy')).selectedItem.name
    return dict(TAPER_BY)[name]


def _read_spec(inputs, sketch=None):
    """Spec dict with internal-unit values plus the expressions typed.
    sketch: needed to convert the triad transform into sketch space."""
    val = lambda k: adsk.core.ValueCommandInput.cast(inputs.itemById(k))
    spec = {
        'mode': adsk.core.DropDownCommandInput.cast(inputs.itemById('mode')).selectedItem.name,
        'hand': 'right' if adsk.core.DropDownCommandInput.cast(
            inputs.itemById('hand')).selectedItem.name.startswith('Right') else 'left',
        'flip': adsk.core.BoolValueCommandInput.cast(inputs.itemById('flip')).value,
        'taperBy': _taper_by(inputs),
        'expr': {},
    }
    for k in ('radius', 'endRadius', 'pitch', 'height', 'turns', 'taper', 'startAngle'):
        spec[k] = val(k).value
        spec['expr'][k] = val(k).expression
    _read_placement(spec, inputs, sketch)
    return spec


def _read_placement(spec, inputs, sketch):
    """Triad, driving points and path, onto the spec. Shared by both dialogs;
    sketch is None for a feature, whose placement the caller applies instead."""
    tri = _find(inputs, 'placement')
    if tri and sketch is not None:
        global _triad_last
        tri = adsk.core.TriadCommandInput.cast(tri)
        axis = None
        for label, key in AXIS_CHOICES:
            if adsk.core.DropDownCommandInput.cast(_find(inputs, 'axis')).selectedItem.name == label:
                axis = key
        # positionTransform reports identity on current builds; transform is fine as scaling is hidden.
        # A hidden triad has been reset, so keep using the last one we saw.
        if _find(inputs, 'placement_grp').isVisible:
            T = tri.transform.copy()
            _triad_last = T
        else:
            T = _triad_last.copy()
        spec['axis'] = axis
        spec['triad'] = list(T.asArray())
        center, start = _effective_point(inputs, 'center'), _effective_point(inputs, 'start')
        spec['centerToken'] = center.entityToken if center else None
        spec['startToken'] = start.entityToken if start else None
        _place_in_sketch(spec, sketch, center, start)
    if sketch is not None:
        path = _effective_point(inputs, 'path')
        spec['pathToken'] = path.entityToken if path else None
        _attach_path(spec, sketch.transform, path)


# --------------------------------------------------------------------------
# Variable pitch dialog
# --------------------------------------------------------------------------
def _find(inputs, cid, depth=0):
    """An input by id, looking inside groups as well; itemById on the top
    level collection does not see into them. Tables are not descended: their
    input collection leads back to the top and the walk never ends."""
    hit = inputs.itemById(cid)
    if hit or depth > 4:
        return hit
    for i in range(inputs.count):
        g = adsk.core.GroupCommandInput.cast(inputs.item(i))
        if g:
            hit = _find(g.children, cid, depth + 1)
            if hit:
                return hit
    return None


def _clip(name):
    """Full path to a tool clip image, or empty if it is not there. A picture
    that failed to ship must not take the dialog down with it."""
    p = os.path.join(TOOLCLIPS, name)
    return p if os.path.exists(p) else ''


def _tip(inputs, cid, tip, description=None, clip=None):
    """Set the hover text on an input. Fusion shows `tooltip` straight away,
    then the tool clip picture and `tooltipDescription` once the pointer rests
    there, so the first is the heading and the rest is the explanation."""
    inp = _find(inputs, cid)
    if not inp:
        return
    inp.tooltip = tip
    if description:
        inp.tooltipDescription = description
    if clip:
        inp.toolClipFilename = _clip(clip)


def _cell(inputs, cid):
    """A station table cell, found by where it sits in the table rather than by
    name: pitch<k> and radius<k> are on row k, turns<j> (the gap after station
    j) on row j+1. Looking a cell up by id through the table's own collection
    does not work once the table is inside a group."""
    table = adsk.core.TableCommandInput.cast(_find(inputs, 'table'))
    if not table:
        return None
    base = cid.rstrip('0123456789')
    k = int(cid[len(base):])
    row, col = {'turns': (k + 1, 1), 'pitch': (k, 2), 'radius': (k, 3)}[base]
    return table.getInputAtPosition(row, col)


def _blend_of(inputs):
    dd = adsk.core.DropDownCommandInput.cast(_find(inputs, 'blend'))
    return dict(BLENDS)[dd.selectedItem.name]


def _add_var_inputs(inputs, spec=None, context='create', sketch=None):
    """The variable pitch dialog, in three groups: where the helix sits, the
    station table, and how it winds. Pickers first, so selection runs top to
    bottom the way it does in the other dialog."""
    global _pending_selections, _var_focus
    _pending_selections, _var_focus = [], None
    lu = app.activeProduct.unitsManager.defaultLengthUnits
    spec = spec or {}
    stations = spec.get('stations') or _default_stations()
    n = max(2, min(MAX_STATIONS, len(stations)))

    pos = inputs.addGroupCommandInput('position_grp', 'Position (optional)')
    pos.isExpanded = True
    pos.tooltip = 'Where the helix sits in the model'
    pos.tooltipDescription = (
        'All of these are optional. Leave them alone and the helix is built on the XY '
        'plane with its axis through the origin.')
    _add_pickers(pos.children, spec, context, with_path=False,
                 start_tip='Optional. Where the helix starts: sets the start angle and the '
                           'height it starts at. The radii come from the table.')
    _add_placement(pos.children, spec, sketch)

    grp = inputs.addGroupCommandInput('stations_grp', 'Stations')
    grp.isExpanded = True
    grp.tooltip = 'Where along the helix you set the pitch and radius'
    grp.tooltipDescription = (
        'A station is one place along the helix. You give it a pitch and a radius, and '
        'between two stations both values blend from one to the other. That is what makes '
        'the pitch variable.<br><br>Click into any row and the preview shows you where that '
        'station is, lights up the run of helix that row controls, and writes out what it '
        'is doing.')
    grp.toolClipFilename = _clip('stations.png')
    ch = grp.children
    cnt = ch.addIntegerSpinnerCommandInput('stations', 'Stations', 2, MAX_STATIONS, 1, n)
    cnt.isEnabled = context != 'edit_feature'   # a feature's parameters are fixed
    cnt.tooltip = 'How many stations the helix has'
    cnt.tooltipDescription = (
        'Two gives a plain helix. Add more and the pitch can change several times along '
        'the length, which is what a progressive spring or a timing screw needs.<br><br>'
        'This is greyed out when you edit a finished feature. Fusion decides which '
        'parameters a feature owns at the moment it is created and will not let them '
        'change afterwards, so a different number of stations means a new helix.')
    cnt.toolClipFilename = _clip('stations.png')
    bl = ch.addDropDownCommandInput('blend', 'Blend', adsk.core.DropDownStyles.TextListDropDownStyle)
    cur_blend = spec.get('blend', 'smooth')
    for label, key in BLENDS:
        bl.listItems.add(label, key == cur_blend)
    bl.tooltip = 'How the pitch changes between one station and the next'
    bl.tooltipDescription = (
        'Smooth eases from one pitch to the other, so a body swept along the curve has no '
        'crease where the pitch changes. This is the one to use for a spring.<br><br>'
        'Linear goes straight from one pitch to the other, which is what SOLIDWORKS does. '
        'It leaves a slight kink at each station.<br><br>The choice changes the height, '
        'because the height is the area under the pitch and the two shapes cover '
        'different areas.')
    bl.toolClipFilename = _clip('blend.png')

    table = ch.addTableCommandInput('table', 'Stations', 4, '1:4:3:3')
    table.minimumVisibleRows = 3
    table.maximumVisibleRows = n + 1     # no blank rows reserved under the table
    table.tablePresentationStyle = adsk.core.TablePresentationStyles.itemBorderTablePresentationStyle
    tc = table.commandInputs
    for c, (hid, label) in enumerate((('h_station', 'Station'), ('h_turns', 'Turns from previous'),
                                      ('h_pitch', 'Pitch'), ('h_radius', 'Radius'))):
        table.addCommandInput(tc.addTextBoxCommandInput(hid, '', '<b>%s</b>' % label, 1, True), 0, c)
    # turns<k> is the gap after station k, shown on the row of station k+1 so
    # that reading down each row says how far on from the one above it sits.
    gaps = [st['turns'] for st in stations if 'turns' in st] or [1.0]
    for k in range(1, n + 1):
        st = stations[min(k - 1, len(stations) - 1)]
        _add_station_row(table, k,
                         _seed(spec, 'turns%d' % (k - 1), gaps[min(k - 2, len(gaps) - 1)]) if k >= 2 else None,
                         _seed(spec, 'pitch%d' % k, st.get('pitch', 1.0)),
                         _seed(spec, 'radius%d' % k, st.get('radius', 2.0)), lu)
    tb = ch.addTextBoxCommandInput('readout', '', '', 1, True)
    tb.isFullWidth = True

    ends = inputs.addGroupCommandInput('ends_grp', 'Ends')
    ends.isExpanded = True
    ends.tooltip = 'Extra coils on the ends of the helix'
    ends.tooltipDescription = (
        'The same idea as the coil ends in Inventor. Use this for the closed ends of a '
        'spring, or for a dwell on a timing screw.')
    ends.toolClipFilename = _clip('ends.png')
    ec = ends.children
    for side, label in (('start', 'Start'), ('end', 'End')):
        dd = ec.addDropDownCommandInput(side + 'Type', label, adsk.core.DropDownStyles.TextListDropDownStyle)
        cur = spec.get(side + 'Type', 'natural')
        for l, key in END_TYPES:
            dd.listItems.add(l, key == cur)
        dd.isEnabled = context != 'edit_feature'   # decides which parameters the feature owns
        ec.addValueInput(side + 'Pitch', label + ' flat pitch', lu, _seed(spec, side + 'Pitch', 0.0))
        ec.addValueInput(side + 'Flat', label + ' flat turns', '', _seed(spec, side + 'Flat', 0.75))
        ec.addValueInput(side + 'Blend', label + ' transition turns', '', _seed(spec, side + 'Blend', 0.5))

    wnd = inputs.addGroupCommandInput('winding_grp', 'Winding')
    wnd.isExpanded = True
    wc = wnd.children
    wc.addValueInput('startAngle', 'Start Angle', 'deg', _seed(spec, 'startAngle', 0.0))
    hd = wc.addDropDownCommandInput('hand', 'Direction', adsk.core.DropDownStyles.TextListDropDownStyle)
    rh = spec.get('hand', 'right') == 'right'
    hd.listItems.add('Right hand', rh)
    hd.listItems.add('Left hand', not rh)
    fl = wc.addBoolValueInput('flip', 'Flip direction', True, '', bool(spec.get('flip')))
    fl.tooltip = 'Run the helix down the axis instead of up'
    fl.tooltipDescription = (
        'Everything else stays put. Station 1 is still the start and a right hand helix is '
        'still right hand. It builds downwards from where it starts.')

    if context == 'create' and _in_sketch():
        inputs.addBoolValueInput('asFeature', 'Finish sketch and create parametric feature',
                                 True, '', False)

    _add_var_tooltips(inputs)
    _apply_var_visibility(inputs)
    _update_var_readout(inputs)


def _add_var_tooltips(inputs):
    """Hover text for everything in the variable pitch dialog. The pickers and
    the placement triad are shared with the other command, so their wording is
    set here rather than where they are built."""
    _tip(inputs, 'start', 'A point for the helix to begin at',
         'It sets the angle the helix starts at and the height it starts from. The radius '
         'still comes from the table, because one point cannot set a different radius at '
         'every station.')
    _add_shared_tooltips(inputs)
    _tip(inputs, 'table', 'One row for each station, in order along the helix',
         'Row 1 is where the helix starts, which is why its turns cell reads start rather '
         'than a number.', 'pitch.png')
    _tip(inputs, 'readout', 'The height and the total turns, worked out from the table',
         'Height is the area under the pitch curve. Multiplying the pitch by the turns only '
         'gives the right answer where the pitch holds steady.', 'pitch-rise.png')
    for side, label, into in (('start', 'Start', 'into'), ('end', 'End', 'out of')):
        _tip(inputs, side + 'Type', 'How the %s of the helix is finished' % label.lower(),
             'Natural begins right at the station, with nothing added.<br><br>Flat adds a '
             'run of coil at a pitch you choose, then eases %s the station over the '
             'transition turns.' % into, 'ends.png')
        _tip(inputs, side + 'Pitch', 'How far the flat run climbs in one turn',
             'Set it to zero for a true flat, which is what a dwell on a timing screw '
             'needs. For a closed spring end use the wire diameter, or the coils will '
             'pass through each other when you sweep it.', 'ends.png')
        _tip(inputs, side + 'Flat', 'How many turns the flat run goes on for',
             'On a spring this is the dead coil that sits against the seat. On a timing '
             'screw it is how long the container is held still for.', 'ends.png')
        _tip(inputs, side + 'Blend', 'How many turns it takes to ease %s the station' % into,
             'Spread over more turns it changes more gently. Too short and the coil has to '
             'bend sharply to catch up.', 'ends.png')
    _tip(inputs, 'winding_grp', 'Which way the helix turns and where it starts',
         'None of this changes the pitch or the height, only the direction it winds and '
         'the point on the circle it begins from.')


def _add_station_row(table, k, turns, pitch, radius, lu):
    """Row k of the table, station k. The seeds are ValueInputs; turns is
    ignored for row 1, which is the start and has nothing before it."""
    tc = table.commandInputs
    table.addCommandInput(tc.addTextBoxCommandInput('s%d' % k, '', '<b>%d</b>' % k, 1, True), k, 0)
    if k == 1:
        cell = tc.addTextBoxCommandInput('t_start', '', 'start', 1, True)
        cell.tooltip = 'This row is where the helix starts'
        cell.tooltipDescription = ('There is no run before it, so there is nothing to '
                                   'measure in turns here.')
    else:
        cell = tc.addValueInput('turns%d' % (k - 1), 'Turns', '', turns)
        cell.tooltip = 'Turns from station %d to station %d' % (k - 1, k)
        cell.tooltipDescription = ('The helix goes this far round between those two '
                                   'stations, blending the pitch and the radius across it.')
        cell.toolClipFilename = _clip('turns.png')
    table.addCommandInput(cell, k, 1)
    p = tc.addValueInput('pitch%d' % k, 'Pitch', lu, pitch)
    p.tooltip = 'How far the helix climbs in one turn, at station %d' % k
    p.tooltipDescription = (
        'Pitch is a rate, measured at this one point. Between two stations it blends to the '
        'next value, so the run rises by the average of the two and climbs less than the '
        'larger pitch would suggest on its own.')
    p.toolClipFilename = _clip('pitch-rise.png')
    table.addCommandInput(p, k, 2)
    r = tc.addValueInput('radius%d' % k, 'Radius', lu, radius)
    r.tooltip = 'Distance from the axis at station %d' % k
    r.tooltipDescription = ('Give two stations different radii and the helix tapers '
                            'between them, like a conical spring.')
    r.toolClipFilename = _clip('radius.png')
    table.addCommandInput(r, k, 3)


def _size_var_dialog(cmd):
    """The station table needs more width than Fusion gives a dialog by default."""
    try:
        cmd.setDialogInitialSize(520, 700)
        cmd.setDialogMinimumSize(460, 360)
    except Exception:
        _log('Helix3D could not size the dialog:\n' + traceback.format_exc())


def _rebuild_station_table(inputs):
    """Match the rows to the station count. Rows are made as the count goes up,
    seeded from the row above, and deleted as it comes down, which takes their
    cells with them. Nothing exists that is not on show: a cell outside a row
    draws itself as a loose input under the table."""
    table = adsk.core.TableCommandInput.cast(_find(inputs, 'table'))
    n = adsk.core.IntegerSpinnerCommandInput.cast(_find(inputs, 'stations')).value
    lu = app.activeProduct.unitsManager.defaultLengthUnits
    have = table.rowCount - 1          # less the header row
    while have > n:
        table.deleteRow(have)
        have -= 1
    table.maximumVisibleRows = n + 1
    while have < n:
        prev = lambda col: adsk.core.ValueCommandInput.cast(table.getInputAtPosition(have, col))
        above = prev(1)                # row 1 holds the "start" label there, not a value
        turns = (adsk.core.ValueInput.createByString(above.expression) if above
                 else adsk.core.ValueInput.createByReal(1.0))
        _add_station_row(table, have + 1, turns,
                         adsk.core.ValueInput.createByString(prev(2).expression),
                         adsk.core.ValueInput.createByString(prev(3).expression), lu)
        have += 1


def _var_focus_range(spec):
    """(from, to) in turns for the run the dialog is on: the stretch from the
    station above down to this row's own station, which is exactly what the
    row's turns value measures.

    Clicking a cell tells us the row and nothing else, so every cell in a row
    has to mean the same thing, and the turns value is the only thing in a row
    that describes a run. Row 1 has nothing above it, which is why its turns
    cell says start: it gets a marker and no run."""
    if not _var_focus or spec.get('mode') != MODE_VAR:
        return None
    xs, _, _ = _station_axes(_expanded_stations(spec))
    o = 2 if spec.get('startType') == 'flat' else 0      # user station k sits at xs[k-1+o]
    n = len(spec['stations'])
    at = lambda k: xs[k - 1 + o]
    kind = _var_focus[0]
    if kind == 'row':
        k = _var_focus[1]
        if k == 1:
            return (0.0, at(1)) if o else None      # the flat start, if there is one
        if 2 <= k <= n:
            return at(k - 1), at(k)
    elif kind == 'start':
        return (0.0, at(1)) if o else None
    elif kind == 'end':
        return (at(n), xs[-1]) if spec.get('endType') == 'flat' else None
    return None


def _var_focus_station(spec):
    """Turns at the station the dialog is on, so it can be marked."""
    if not _var_focus or spec.get('mode') != MODE_VAR:
        return None
    xs, _, _ = _station_axes(_expanded_stations(spec))
    o = 2 if spec.get('startType') == 'flat' else 0
    n = len(spec['stations'])
    kind = _var_focus[0]
    if kind == 'row' and 1 <= _var_focus[1] <= n:
        return xs[_var_focus[1] - 1 + o]
    if kind == 'start':
        return 0.0
    if kind == 'end':
        return xs[-1]
    return None


def _helix_point(spec, u):
    """The point at u turns along the helix, in sketch space. A clamped spline
    starts and ends on its outer control points, so a hair of curve either side
    of u gives the point exactly."""
    sts = _expanded_stations(spec)
    total = _station_axes(sts)[0][-1]
    eps = max(total, 1e-6) * 1e-3
    tail = u >= total - eps
    lo = max(total - eps, 0.0) if tail else min(max(u, 0.0), max(total - eps, 0.0))
    P, _ = var_helix_nurbs(sts, spec['startAngle'], spec['hand'] == 'right',
                           spec.get('blend', 'smooth') == 'smooth', bool(spec.get('flip')),
                           PREVIEW_SAMPLES_PER_TURN, (lo, lo + eps))
    return adsk.core.Point3D.create(*(P[-1] if tail else P[0]))


def _placed(spec, p):
    """A helix-local point with the helix's own placement applied."""
    xf = spec.get('xform')
    if xf:
        M = adsk.core.Matrix3D.create()
        M.setWithArray(xf)
        p.transformBy(M)
    return p


def _point_at(spec, u):
    return _placed(spec, _helix_point(spec, u))


def _focus_size(spec):
    """Marker and text sized off the helix, so they stay in proportion."""
    r = _helix_radius(spec)
    return max(r * 0.15, 0.125), max(r * 0.065, 0.06)   # cross arm, text height


def _helix_radius(spec):
    return max([st.get('radius', 0.0) for st in spec['stations']] or [1.0])


def _label_anchor(spec):
    """Where the note sits: under the bottom of the helix, off to one side.
    It stays put whichever row is picked, so the eye does not have to chase it
    around the model."""
    sts = _expanded_stations(spec)
    xs, ps, _ = _station_axes(sts)
    rise = _Ramp(xs, ps, spec.get('blend', 'smooth') == 'smooth').total()
    if spec.get('flip'):
        rise = -rise
    r = _helix_radius(spec)
    # Seen at an angle the bottom coil's own ellipse reaches about a radius
    # below its centre, so the note drops a little past that to sit clear.
    gap = max(r * 0.85, 0.45)
    return _placed(spec, adsk.core.Point3D.create(-r, 0.0, min(0.0, rise) - gap))


def _build_highlight(spec):
    """The highlighted stretch as a curve in sketch space, or None."""
    rng = _var_focus_range(spec)
    if not rng or rng[1] - rng[0] <= 1e-9:
        return None
    P, U = var_helix_nurbs(_expanded_stations(spec), spec['startAngle'], spec['hand'] == 'right',
                           spec.get('blend', 'smooth') == 'smooth', bool(spec.get('flip')),
                           PREVIEW_SAMPLES_PER_TURN, rng)
    return _curve_from(P, U, spec.get('xform'))


def _build_marker(spec):
    """(point in sketch space, arm length) for a cross on the row's station."""
    u = _var_focus_station(spec)
    if u is None:
        return None
    return _point_at(spec, u), _focus_size(spec)[0]


def _focus_label(spec):
    """(text, anchor in sketch space, height): what the row is doing to the
    helix, written out on the canvas the way a dimension would be."""
    if not _var_focus or spec.get('mode') != MODE_VAR:
        return None
    n = len(spec['stations'])
    kind = _var_focus[0]
    k = _var_focus[1] if kind == 'row' else (1 if kind == 'start' else n)
    if not 1 <= k <= n:
        return None
    um = app.activeProduct.unitsManager
    lu = um.defaultLengthUnits
    # formatInternalValue gives a value like 6.8333333 its full precision;
    # two places is what the rest of the dialog shows.
    length = lambda v: '%.2f %s' % (um.convert(v, 'cm', lu), lu)
    turns = lambda v: '%.2f' % v
    st = spec['stations'][k - 1]
    rng = _var_focus_range(spec)
    if rng:
        sts = _expanded_stations(spec)
        xs, ps, _ = _station_axes(sts)
        ramp = _Ramp(xs, ps, spec.get('blend', 'smooth') == 'smooth')
        rise = ramp.integral(rng[1]) - ramp.integral(rng[0])
        span, climb = turns(rng[1] - rng[0]), length(rise)
        if kind == 'start':
            head = 'flat end, %s turns into it, rising %s' % (span, climb)
        elif kind == 'end':
            head = 'flat end, %s turns out of it, rising %s' % (span, climb)
        else:
            head = '%s turns on, rising %s' % (span, climb)
    else:
        head = 'where the helix finishes' if kind == 'end' else 'where the helix starts'
    # Three short lines read better on a billboard than two long ones.
    text = 'Station %d\n%s\npitch %s, radius %s' % (
        k, head, length(st.get('pitch', 0.0)), length(st.get('radius', 0.0)))
    return text, _label_anchor(spec), _focus_size(spec)[1]


ORANGE = (255, 140, 0)     # the lit run and the note
BLUE = (0, 140, 255)       # the station cross, the add-in's own preview blue


def _show_highlight(spec, comp, xf):
    """Overlay on the part of the helix the dialog is on: the run the row's
    turns value measures, a cross on the row's own station, and a note saying
    what that row is doing. Lives in the preview transaction with everything
    else, so it goes when the dialog does."""
    try:
        curve = _build_highlight(spec)
        mark = _build_marker(spec)
        label = _focus_label(spec)
    except HelixError:
        return
    if curve is None and mark is None and label is None:
        return
    g = comp.customGraphicsGroups.add()
    tint = lambda rgb: adsk.fusion.CustomGraphicsSolidColorEffect.create(
        adsk.core.Color.create(*(rgb + (255,))))
    paint = tint(ORANGE)

    def draw(geom, weight, rgb=ORANGE):
        e = g.addCurve(geom)
        if not e:
            return
        e.weight = weight
        e.color = tint(rgb)
        try:
            e.depthPriority = 1     # over the curve it sits on
        except Exception:
            pass

    if curve is not None:
        curve.transformBy(xf)
        draw(curve, 3.5)
    if mark is not None:
        p, size = mark
        p.transformBy(xf)
        for dx, dy, dz in ((size, 0, 0), (0, size, 0), (0, 0, size)):
            draw(adsk.core.Line3D.create(
                adsk.core.Point3D.create(p.x - dx, p.y - dy, p.z - dz),
                adsk.core.Point3D.create(p.x + dx, p.y + dy, p.z + dz)), 2.1, BLUE)
    if label is not None:
        text, p, height = label
        p.transformBy(xf)
        M = adsk.core.Matrix3D.create()
        M.translation = adsk.core.Vector3D.create(p.x, p.y, p.z)
        t = g.addText(text, 'Arial', height, M)
        if t:
            t.color = paint
            try:
                # Face the screen, so it stays readable whichever way the model
                # is spun. Without this the note lies flat on the XY plane.
                bb = adsk.fusion.CustomGraphicsBillBoard.create(p)
                bb.billBoardStyle = adsk.fusion.CustomGraphicsBillBoardStyles.ScreenBillBoardStyle
                t.billBoarding = bb
            except Exception:
                _log('Helix3D label billboard failed:\n' + traceback.format_exc())
            try:
                t.depthPriority = 1
            except Exception:
                pass


class VarPoll(adsk.core.CustomEventHandler):
    """Fusion fires nothing when a table row is clicked, only when a value
    changes. A thread ticks this event while the dialog is open; here, on the
    main thread, the selected row is compared with the last one seen and the
    preview is redrawn when it has moved."""
    def notify(self, args):
        global _var_last_row, _var_focus
        try:
            cmd = _var_cmd
            if cmd is None or not cmd.isValid:
                return
            table = adsk.core.TableCommandInput.cast(_find(cmd.commandInputs, 'table'))
            if not table:
                return
            row = table.selectedRow
            if row == _var_last_row:
                return
            _var_last_row = row
            if row >= 1:
                _var_focus = ('row', row)
                cmd.doExecutePreview()
        except Exception:
            _log('Helix3D row watch failed:\n' + traceback.format_exc())


def _start_var_poll(cmd):
    global _var_cmd, _var_poll_stop, _var_last_row
    _stop_var_poll()
    _var_cmd, _var_last_row = cmd, -2
    stop = threading.Event()
    _var_poll_stop = stop

    def tick():
        while not stop.wait(0.15):
            try:
                app.fireCustomEvent(VAR_POLL_EVT)
            except Exception:
                break
    threading.Thread(target=tick, daemon=True).start()


def _stop_var_poll():
    global _var_cmd, _var_poll_stop
    if _var_poll_stop is not None:
        _var_poll_stop.set()
    _var_cmd, _var_poll_stop = None, None


def _selected_row(inputs):
    """The table row the user is in, so a value typed into a cell does not get
    overwritten by the row watcher a moment later."""
    table = adsk.core.TableCommandInput.cast(_find(inputs, 'table'))
    return table.selectedRow if table else -2


def _end_type(inputs, side):
    dd = adsk.core.DropDownCommandInput.cast(_find(inputs, side + 'Type'))
    return dict(END_TYPES)[dd.selectedItem.name]


def _apply_var_visibility(inputs):
    has_start = _effective_point(inputs, 'start') is not None
    has_center = _effective_point(inputs, 'center') is not None
    _find(inputs, 'startAngle').isVisible = not has_start
    _apply_placement_visibility(inputs, not (has_center and has_start))
    for side in ('start', 'end'):
        flat = _end_type(inputs, side) == 'flat'
        for f in END_FIELDS:
            _find(inputs, side + f).isVisible = flat


def _update_var_readout(inputs):
    """Height and turns fall out of the table, so show them rather than asking
    for them. It doubles as where a bad station gets reported."""
    tb = adsk.core.TextBoxCommandInput.cast(_find(inputs, 'readout'))
    if not tb:
        return
    um = app.activeProduct.unitsManager
    try:
        spec = _read_var_spec(inputs)
        sts = _expanded_stations(spec)
        height = var_helix_height(sts, spec['blend'] == 'smooth')
        turns = sum(st.get('turns', 0.0) for st in sts[:-1])
        tb.formattedText = 'Height %s over %s turns' % (
            um.formatInternalValue(height, um.defaultLengthUnits, True),
            um.formatInternalValue(turns, '', False))
    except HelixError as e:
        tb.formattedText = '<b>%s</b>' % e
    except Exception:
        tb.formattedText = ''


def _read_var_spec(inputs, sketch=None):
    """Spec dict for the variable pitch dialog, in internal units, with the
    expressions typed into each cell."""
    n = adsk.core.IntegerSpinnerCommandInput.cast(_find(inputs, 'stations')).value
    spec = {
        'mode': MODE_VAR,
        'hand': 'right' if adsk.core.DropDownCommandInput.cast(
            _find(inputs, 'hand')).selectedItem.name.startswith('Right') else 'left',
        'flip': adsk.core.BoolValueCommandInput.cast(_find(inputs, 'flip')).value,
        'blend': _blend_of(inputs),
        'stations': [],
        'expr': {},
    }
    for i in range(n):
        st = {}
        for base in ('turns', 'pitch', 'radius'):
            if base == 'turns' and i == n - 1:
                continue
            key = '%s%d' % (base, i + 1)
            box = adsk.core.ValueCommandInput.cast(_cell(inputs, key))
            if box is None:
                raise HelixError('The station table is missing its %s cell.' % key)
            st[base] = box.value
            spec[key] = box.value
            spec['expr'][key] = box.expression
        spec['stations'].append(st)
    sa = adsk.core.ValueCommandInput.cast(_find(inputs, 'startAngle'))
    spec['startAngle'] = sa.value
    spec['expr']['startAngle'] = sa.expression
    for side in ('start', 'end'):
        spec[side + 'Type'] = _end_type(inputs, side)
        if spec[side + 'Type'] == 'flat':
            for f in END_FIELDS:
                box = adsk.core.ValueCommandInput.cast(_find(inputs, side + f))
                spec[side + f] = box.value
                spec['expr'][side + f] = box.expression
    _read_placement(spec, inputs, sketch)
    return spec


def _read_any_spec(inputs, sketch=None):
    """Whichever dialog is open. The variable pitch one is the one with a
    station count."""
    if _find(inputs, 'stations'):
        return _read_var_spec(inputs, sketch)
    return _read_spec(inputs, sketch)


def _place_in_sketch(spec, sketch, center_ent, start_ent):
    """In-sketch placement: the triad (spec['triad'], spec['axis']) unless both
    points are set, in which case the axis is the sketch normal through the
    centre, exactly as for a feature."""
    if center_ent and start_ent:
        M = adsk.core.Matrix3D.create()
    else:
        T = adsk.core.Matrix3D.create()
        T.setWithArray(spec['triad'])
        W = _axis_rotation(spec.get('axis', 'Z'))   # local Z -> chosen triad axis
        W.transformBy(T)                            # then the triad's own placement: W = T * R
        M = _triad_to_sketch(sketch, W)
    _place(spec, M, _model_to_sketch(sketch.transform), center_ent, start_ent)


def _param_ids(mode, taper_by='angle'):
    ids = {
        MODE_RP: ('radius', 'pitch', 'turns', 'taper', 'startAngle'),
        MODE_RH: ('radius', 'turns', 'height', 'taper', 'startAngle'),
        MODE_HP: ('radius', 'height', 'pitch', 'taper', 'startAngle'),
        MODE_SPIRAL: ('radius', 'endRadius', 'turns', 'startAngle'),
        MODE_PP: ('radius', 'pitch', 'taper', 'startAngle'),
        MODE_PR: ('radius', 'turns', 'taper', 'startAngle'),
    }[mode]
    if taper_by == 'radius':
        ids = tuple('endRadius' if i == 'taper' else i for i in ids)
    return ids


TAPER_BY = [('By angle', 'angle'), ('By end radius', 'radius')]


AXIS_CHOICES = [('XY plane (axis = triad Z)', 'Z'),
                ('YZ plane (axis = triad X)', 'X'),
                ('XZ plane (axis = triad Y)', 'Y')]


def _axis_rotation(axis):
    """Rotation taking the helix axis (local Z) onto the chosen triad axis."""
    R = adsk.core.Matrix3D.create()
    o = adsk.core.Point3D.create(0, 0, 0)
    if axis == 'X':
        R.setToRotation(math.radians(90), adsk.core.Vector3D.create(0, 1, 0), o)
    elif axis == 'Y':
        R.setToRotation(math.radians(-90), adsk.core.Vector3D.create(1, 0, 0), o)
    return R


def _station_ids(n):
    """Parameter ids for n stations: the turns to the next station (all but the
    last), the pitch and the radius at each one."""
    ids = []
    for i in range(n):
        if i < n - 1:
            ids.append('turns%d' % (i + 1))
        ids.append('pitch%d' % (i + 1))
        ids.append('radius%d' % (i + 1))
    return tuple(ids)


def _end_param_ids(spec):
    ids = []
    for side in ('start', 'end'):
        if spec.get(side + 'Type') == 'flat':
            ids += [side + f for f in END_FIELDS]
    return tuple(ids)


def _var_param_ids(spec):
    return _station_ids(len(spec['stations'])) + _end_param_ids(spec) + ('startAngle',)


def _expanded_stations(spec):
    """The stations with any flat ends added on. A flat end is a run at the
    end pitch for the flat turns, then the transition turns into the first (or
    out of the last) station. Two stations at one pitch give an exactly flat
    run, so the solver needs nothing new to know about."""
    sts = [dict(st) for st in spec['stations']]
    for side in ('start', 'end'):
        if spec.get(side + 'Type') != 'flat' or not sts:
            continue
        p, flat, blend = (spec.get(side + f, 0.0) for f in END_FIELDS)
        if p < -1e-9:
            raise HelixError('%s flat pitch cannot be negative.' % side.capitalize())
        if flat <= 1e-9 or blend <= 1e-9:
            raise HelixError('%s flat turns and transition turns both have to be more than zero.'
                             % side.capitalize())
        if side == 'start':
            r = sts[0].get('radius', 0.0)
            sts = [{'turns': flat, 'pitch': p, 'radius': r},
                   {'turns': blend, 'pitch': p, 'radius': r}] + sts
        else:
            r = sts[-1].get('radius', 0.0)
            sts[-1]['turns'] = blend
            sts += [{'turns': flat, 'pitch': p, 'radius': r}, {'pitch': p, 'radius': r}]
    return sts


def _var_param_meta(pid):
    """(display name, unit key) for a station parameter, as it reads in the
    Parameters dialog."""
    if pid in PARAM_META:
        return PARAM_META[pid]
    base = pid.rstrip('0123456789')
    i = int(pid[len(base):])
    if base == 'turns':
        return ('Turns %d to %d' % (i, i + 1), '')
    return ('%s %d' % (base.capitalize(), i), 'len')


def _default_stations():
    """A closed end, the coils, then a closed end again: the transition and
    flat ends Inventor's Coil offers, built out of stations."""
    return [{'turns': 1.0, 'pitch': 0.2, 'radius': 2.0},
            {'turns': 3.0, 'pitch': 1.0, 'radius': 2.0},
            {'turns': 1.0, 'pitch': 1.0, 'radius': 2.0},
            {'pitch': 0.2, 'radius': 2.0}]


def _stations_from_values(spec):
    """Gather the flat pitch1/radius1/turns1 values on a spec back into
    spec['stations']."""
    n = len(spec.get('stations') or [])
    if not n:
        return
    out = []
    for i in range(n):
        st = {}
        for base in ('turns', 'pitch', 'radius'):
            k = '%s%d' % (base, i + 1)
            if k in spec:
                st[base] = spec[k]
            elif i < len(spec['stations']) and base in spec['stations'][i]:
                st[base] = spec['stations'][i][base]
        if i == n - 1:
            st.pop('turns', None)
        out.append(st)
    spec['stations'] = out


def _values_from_stations(spec):
    """The other way round: flat ids on the spec, which is what the expression
    dictionary and the custom feature parameters are keyed by."""
    for i, st in enumerate(spec.get('stations') or []):
        for base, value in st.items():
            spec['%s%d' % (base, i + 1)] = value


PARAM_META = {  # id: (display name, unit key)
    'radius': ('Radius', 'len'), 'endRadius': ('End Radius', 'len'),
    'pitch': ('Pitch', 'len'), 'height': ('Height', 'len'),
    'turns': ('Revolutions', ''), 'taper': ('Taper Angle', 'deg'),
    'startAngle': ('Start Angle', 'deg'),
    'startPitch': ('Start flat pitch', 'len'), 'startFlat': ('Start flat turns', ''),
    'startBlend': ('Start transition turns', ''),
    'endPitch': ('End flat pitch', 'len'), 'endFlat': ('End flat turns', ''),
    'endBlend': ('End transition turns', ''),
}


def _show_origin(on):
    """Temporarily light the active component's origin while picking a plane."""
    global _origin_restore
    try:
        des = adsk.fusion.Design.cast(app.activeProduct)
        comp = des.activeComponent
        if on:
            _origin_restore = (comp, comp.isOriginFolderLightBulbOn)
            comp.isOriginFolderLightBulbOn = True
        elif _origin_restore:
            c, prev = _origin_restore
            c.isOriginFolderLightBulbOn = prev
            _origin_restore = None
    except Exception:
        _origin_restore = None


# --------------------------------------------------------------------------
# Feature helpers
# --------------------------------------------------------------------------
def _point_world(entity):
    """World-space Point3D for a sketch point, construction point or vertex."""
    sp = adsk.fusion.SketchPoint.cast(entity)
    if sp:
        return sp.worldGeometry
    cp = adsk.fusion.ConstructionPoint.cast(entity)
    if cp:
        return cp.geometry
    vx = adsk.fusion.BRepVertex.cast(entity)
    if vx:
        return vx.geometry
    return None


def _model_to_sketch(sketch_xf):
    Si = sketch_xf.copy()
    Si.invert()
    return Si


def _place(spec, M, Si, center_ent, start_ent):
    """Finish the placement. M maps helix-local space (axis = local Z) to
    sketch space; Si maps model space to sketch space. A centre point moves
    the local origin onto it; a start point sets radius, start angle and the
    axial offset. Writes spec['xform']; mutates M."""
    if spec['mode'] in PATH_MODES:   # the path places the helix; nothing else does
        spec.pop('xform', None)
        return

    def to_sketch(ent):
        p = _point_world(ent)
        p.transformBy(Si)
        return p
    if center_ent:
        c = to_sketch(center_ent)
        M.translation = adsk.core.Vector3D.create(c.x, c.y, c.z)
    if start_ent:
        Mi = M.copy()
        Mi.invert()
        s = to_sketch(start_ent)
        s.transformBy(Mi)            # start point in helix-local space
        r = math.hypot(s.x, s.y)
        if r > 1e-9:
            if spec['mode'] != MODE_VAR:
                spec['radius'] = r   # a variable pitch helix gets its radii from the table
            spec['startAngle'] = math.atan2(s.y, s.x)
        o = adsk.core.Point3D.create(0, 0, s.z)
        o.transformBy(M)             # slide the origin along the axis to the start height
        M.translation = adsk.core.Vector3D.create(o.x, o.y, o.z)
    if M.isEqualTo(adsk.core.Matrix3D.create()):
        spec.pop('xform', None)
    else:
        spec['xform'] = list(M.asArray())


def _apply_points(Si, spec, center_ent, start_ent):
    """Feature placement: axis = sketch normal through the centre point."""
    _place(spec, adsk.core.Matrix3D.create(), Si, center_ent, start_ent)


def _entity_by_token(token):
    if not token:
        return None
    des = adsk.fusion.Design.cast(app.activeProduct)
    for e in des.findEntityByToken(token):
        if e.isValid:
            return e
    return None


def _dep_entity(cf, dep_id):
    d = cf.dependencies.itemById(dep_id)
    return d.entity if d else None


def _spec_of_feature(cf):
    spec = {'mode': cf.customNamedValues.value('mode') or MODE_RP,
            'hand': cf.customNamedValues.value('hand') or 'right',
            'taperBy': cf.customNamedValues.value('taperBy') or 'angle',
            'flip': cf.customNamedValues.value('flip') == '1',
            'expr': {},
            '_deps': {k: _dep_entity(cf, k) for k in ('center', 'start', 'path')}}
    for i in range(cf.parameters.count):
        p = cf.parameters.item(i)
        spec[p.id] = p.value
        spec['expr'][p.id] = p.expression   # so reopening the dialog keeps it
    if spec['mode'] == MODE_VAR:
        spec['blend'] = cf.customNamedValues.value('blend') or 'smooth'
        spec['startType'] = cf.customNamedValues.value('startType') or 'natural'
        spec['endType'] = cf.customNamedValues.value('endType') or 'natural'
        spec['stations'] = [{} for _ in range(int(cf.customNamedValues.value('stations') or 0))]
        _stations_from_values(spec)
    return spec


def _sketch_of(cf):
    for f in cf.features:
        if isinstance(f, adsk.fusion.Sketch):
            return f
    return None


def _rebuild_feature(cf):
    sk = _sketch_of(cf)
    spec = _spec_of_feature(cf)
    _apply_points(_model_to_sketch(sk.transform), spec, spec['_deps']['center'], spec['_deps']['start'])
    _attach_path(spec, sk.transform, spec['_deps']['path'])
    fs = sk.sketchCurves.sketchFixedSplines.item(0)
    return fs.replaceGeometry(build_curve(spec))


def _touch_sketch(sk):
    """replaceGeometry is not seen as a sketch change by features built on the
    curve (a sweep stays stale until Compute All). Adding and deleting a point
    is, but only as a fresh edit, not from inside the feature's own compute."""
    sk.sketchPoints.add(adsk.core.Point3D.create(0, 0, 0)).deleteMe()


def _draw_preview(comp, curve):
    """Transient custom graphics for a curve in component space. Fusion drops
    them when the preview is aborted or the command ends."""
    g = comp.customGraphicsGroups.add()
    c = g.addCurve(curve)
    c.weight = 2
    c.color = adsk.fusion.CustomGraphicsSolidColorEffect.create(adsk.core.Color.create(0, 140, 255, 255))


def _timeline():
    return adsk.fusion.Design.cast(app.activeProduct).timeline


def _roll_back_for_edit(cf):
    """Put the marker just before cf. Returns True if it is there afterwards."""
    global _editing_rolled
    tl, tlo = _timeline(), cf.timelineObject
    if tl.markerPosition != tlo.index:
        _editing_rolled = True
        tlo.rollTo(True)
    return tl.markerPosition == tlo.index


def _restore_timeline():
    global _editing_rolled
    if not _editing_rolled:
        return
    _editing_rolled = False
    if _editing_restore and _editing_restore.isValid:
        _editing_restore.rollTo(False)
    else:
        _timeline().moveToEnd()


def _spec_of_curve(curve):
    a = curve.attributes.itemByName(ATTR_GROUP, 'spec')
    if not a:
        return None
    spec = json.loads(a.value)
    spec['_deps'] = {k: _entity_by_token(spec.get(k + 'Token')) for k in ('center', 'start', 'path')}
    return spec


def _is_helix_curve(entity):
    try:
        return bool(entity.attributes.itemByName(ATTR_GROUP, 'spec'))
    except Exception:
        return False


UNIT_OF = {'radius': 'len', 'endRadius': 'len', 'pitch': 'len', 'height': 'len',
           'turns': '', 'taper': 'deg', 'startAngle': 'deg',
           'startPitch': 'len', 'startFlat': '', 'startBlend': '',
           'endPitch': 'len', 'endFlat': '', 'endBlend': ''}


def _units():
    return {'len': app.activeProduct.unitsManager.defaultLengthUnits, 'deg': 'deg', '': ''}


def _unit_of(pid):
    """Unit key for a parameter id. Station ids carry a number on the end
    (pitch3, turns2); everything else is looked up as it stands."""
    return UNIT_OF[pid.rstrip('0123456789') if pid not in UNIT_OF else pid]


def _evaluate_spec(spec, sketch):
    """Bring a stored in-sketch spec up to date: expressions against the
    current user parameters, placement against the current centre / start
    points, and the path's current shape. Mutates spec."""
    um = app.activeProduct.unitsManager
    units = _units()
    for k, expr in spec.get('expr', {}).items():
        try:
            spec[k] = um.evaluateExpression(expr, units[_unit_of(k)])
        except Exception:
            pass  # a referenced parameter went away; keep the last value
    if spec['mode'] == MODE_VAR:
        _stations_from_values(spec)
    if spec.get('triad'):
        _place_in_sketch(spec, sketch, _entity_by_token(spec.get('centerToken')),
                         _entity_by_token(spec.get('startToken')))
    _attach_path(spec, sketch.transform, _entity_by_token(spec.get('pathToken')))


def _curve_differs(a, b, tol=1e-7):
    ca, cb = a.controlPoints, b.controlPoints
    return len(ca) != len(cb) or any(p.distanceTo(q) > tol for p, q in zip(ca, cb))


def _refresh_sketch_helices():
    """Called after any command finishes: rebuild in-sketch helices whose
    expressions, driving points or path now give a different curve."""
    des = adsk.fusion.Design.cast(app.activeProduct)
    if not des:
        return
    for attr in des.findAttributes(ATTR_GROUP, 'spec'):
        curve = adsk.fusion.SketchFixedSpline.cast(attr.parent)
        if not curve or not curve.isValid:
            continue
        spec = json.loads(attr.value)
        _evaluate_spec(spec, curve.parentSketch)
        try:
            new = build_curve(spec)
        except HelixError:
            continue   # its path is gone; leave the curve as it is
        if _curve_differs(curve.geometry, new):
            curve.replaceGeometry(new)
            attr.value = json.dumps(_storable(spec))


def _storable(spec):
    return {k: v for k, v in spec.items() if not k.startswith('_')}


def _store_spec_on_curve(curve, spec):
    curve.attributes.add(ATTR_GROUP, 'spec', json.dumps(_storable(spec)))


class ComputeHandler(adsk.fusion.CustomFeatureEventHandler):
    def notify(self, args):
        try:
            cf = adsk.fusion.CustomFeatureEventArgs.cast(args).customFeature
            _rebuild_feature(cf)
            if not _touching:
                _touch_pending.add(cf.entityToken)
                app.fireCustomEvent(TOUCH_EVT)   # handled once Fusion is idle again
        except Exception:
            _log('Helix3D compute failed:\n' + traceback.format_exc())


class DeferredTouch(adsk.core.CustomEventHandler):
    """Runs after the compute pass that rebuilt a helix: nudge its sketch so
    features built on the curve recompute (see _touch_sketch)."""
    def notify(self, args):
        global _touching
        try:
            if ui.activeCommand in OUR_COMMANDS:
                return   # left pending; the next compute fires again
            des = adsk.fusion.Design.cast(app.activeProduct)
            tokens = set(_touch_pending)
            _touch_pending.clear()
            _touching = True
            for tok in tokens:
                for e in des.findEntityByToken(tok):
                    cf = adsk.fusion.CustomFeature.cast(e)
                    sk = _sketch_of(cf) if cf and cf.isValid else None
                    if sk and sk.isValid:
                        _touch_sketch(sk)
        except Exception:
            _log('Helix3D deferred touch failed:\n' + traceback.format_exc())
        finally:
            _touching = False


# --------------------------------------------------------------------------
# Shared command plumbing
# --------------------------------------------------------------------------
class TriadActivate(adsk.core.CommandEventHandler):
    """Push the stored placement onto the triad after the dialog is displayed;
    setting it during commandCreated gets reset when the triad first draws."""
    def notify(self, args):
        global _pending_triad
        try:
            if _pending_triad is None:
                return
            inputs = adsk.core.CommandEventArgs.cast(args).command.commandInputs
            tri = adsk.core.TriadCommandInput.cast(_find(inputs, 'placement'))
            if tri:
                tri.transform = _pending_triad
        except Exception:
            _log('Helix3D triad activate failed:\n' + traceback.format_exc())
        finally:
            _pending_triad = None


class InputChanged(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        try:
            args = adsk.core.InputChangedEventArgs.cast(args)
            # args.inputs is whichever collection the changed input sits in, which
            # for a table cell is the table's own. Always work from the top level.
            inputs = adsk.core.Command.cast(args.firingEvent.sender).commandInputs
            cid = args.input.id
            if _touch_armed and cid in ('center', 'start', 'path'):
                _touched.add(cid)
            if cid == 'center':
                _triad_follow_center(inputs)
            if _find(inputs, 'stations'):      # the variable pitch dialog
                global _var_focus, _var_last_row
                base = cid.rstrip('0123456789')
                if cid == 'stations':
                    _rebuild_station_table(inputs)
                    _var_focus = None
                elif cid == 'table':                # a row was clicked
                    row = adsk.core.TableCommandInput.cast(args.input).selectedRow
                    if row >= 1:
                        _var_focus, _var_last_row = ('row', row), row
                elif base != cid and base in ('pitch', 'radius'):
                    _var_focus = ('row', int(cid[len(base):]))
                    _var_last_row = _selected_row(inputs)
                elif base != cid and base == 'turns':   # turns<j> sits on row j+1
                    _var_focus = ('row', int(cid[len(base):]) + 1)
                    _var_last_row = _selected_row(inputs)
                elif cid.startswith('start') and cid != 'startAngle':
                    # An Ends field was changed: show that end rather than
                    # whichever table row happened to be picked last. Keeping
                    # the row in step stops the row watcher taking it back.
                    _var_focus, _var_last_row = ('start',), _selected_row(inputs)
                elif cid.startswith('end'):
                    _var_focus, _var_last_row = ('end',), _selected_row(inputs)
                _apply_var_visibility(inputs)
                _update_var_readout(inputs)
                _advance_focus(inputs, cid)
                return
            global _seeded_end_radius
            if cid == 'taperBy' and _taper_by(inputs) == 'radius' and not _seeded_end_radius:
                # First switch to an end radius in this dialog: start from "no taper".
                _seeded_end_radius = True
                er = adsk.core.ValueCommandInput.cast(inputs.itemById('endRadius'))
                er.expression = adsk.core.ValueCommandInput.cast(inputs.itemById('radius')).expression
            if cid in ('mode', 'center', 'start', 'path', 'taperBy'):
                _apply_mode_visibility(inputs)
            if cid == 'mode':
                _focus_first_empty_picker(inputs)   # the pickers just changed
            _advance_focus(inputs, cid)
        except Exception:
            _log('Helix3D inputChanged failed:\n' + traceback.format_exc())


def _picked(inputs, sid):
    sel = adsk.core.SelectionCommandInput.cast(inputs.itemById(sid))
    return sel.selection(0).entity if sel and sel.selectionCount else None


def _focus_first_empty_picker(inputs):
    """Hiding a picker that holds selection focus leaves focus on the hidden
    input, so the one that just appeared looks disabled until it is clicked.
    Hand focus to the first visible picker that still needs something."""
    visible = []
    for sid in PICKERS:
        p = inputs.itemById(sid)
        if p and p.isVisible:
            visible.append(adsk.core.SelectionCommandInput.cast(p))
    if not visible or any(p.hasFocus for p in visible):
        return
    for p in visible:
        if p.selectionCount == 0:
            p.hasFocus = True
            return


def _advance_focus(inputs, changed_id):
    """Each picker takes one entity, so once it is filled hand selection focus
    to the next visible picker. After the last one Fusion is left to its own
    devices: the API can't give a value box focus, and releasing picker focus
    just sends it to the canvas."""
    if changed_id not in PICKERS:
        return
    sel = adsk.core.SelectionCommandInput.cast(inputs.itemById(changed_id))
    if not sel or sel.selectionCount == 0:
        return
    for sid in PICKERS[PICKERS.index(changed_id) + 1:]:
        nxt = inputs.itemById(sid)
        if nxt and nxt.isVisible:
            adsk.core.SelectionCommandInput.cast(nxt).hasFocus = True
            return


def _triad_follow_center(inputs):
    """Sit the triad on the centre point so its position isn't misleading."""
    global _triad_last
    tri = adsk.core.TriadCommandInput.cast(_find(inputs, 'placement'))
    c = _effective_point(inputs, 'center')
    if not tri or c is None:
        return
    T = tri.transform.copy()
    p = _point_world(c)
    T.translation = adsk.core.Vector3D.create(p.x, p.y, p.z)
    tri.transform = T
    _triad_last = T


def _remember_placement_group(inputs):
    grp = _find(inputs, 'placement_grp')
    if grp and _settings.get('placementExpanded', True) != grp.isExpanded:
        _settings['placementExpanded'] = grp.isExpanded
        _save_settings()


def _wire(cmd, execute_handler, activate=None, destroy=None, preview=None):
    for ev, h in ((cmd.execute, execute_handler), (cmd.inputChanged, InputChanged()),
                  (cmd.activate, activate), (cmd.destroy, destroy),
                  (cmd.executePreview, preview)):
        if h:
            ev.add(h)
            _handlers.append(h)


# --------------------------------------------------------------------------
# Create command
# --------------------------------------------------------------------------
def _create_sketch(comp, plane, spec, center_ent=None, start_ent=None, path_ent=None,
                   per_turn=SAMPLES_PER_TURN):
    sk = comp.sketches.add(plane)
    sk.name = 'Variable Helix' if spec['mode'] == MODE_VAR else 'Helix'
    _apply_points(_model_to_sketch(sk.transform), spec, center_ent, start_ent)
    _attach_path(spec, sk.transform, path_ent)
    sk.sketchCurves.sketchFixedSplines.addByNurbsCurve(build_curve(spec, per_turn))
    return sk


def _create_feature(comp, plane, spec, center_ent=None, start_ent=None, path_ent=None):
    units = _units()
    var = spec['mode'] == MODE_VAR
    sk = _create_sketch(comp, plane, spec, center_ent, start_ent, path_ent)

    cfi = comp.features.customFeatures.createInput(_def_var if var else _def)
    pids = _var_param_ids(spec) if var else _param_ids(spec['mode'], spec.get('taperBy', 'angle'))
    for pid in pids:
        name, ukey = _var_param_meta(pid) if var else PARAM_META[pid]
        cfi.addCustomParameter(pid, name, adsk.core.ValueInput.createByString(spec['expr'][pid]),
                               units[ukey], True)
    cfi.addDependency('plane', plane)
    if center_ent:
        cfi.addDependency('center', center_ent)
    if start_ent:
        cfi.addDependency('start', start_ent)
    if path_ent:
        cfi.addDependency('path', path_ent)
    cfi.setStartAndEndFeatures(sk, sk)
    cf = comp.features.customFeatures.add(cfi)
    cf.customNamedValues.addOrSetValue('mode', spec['mode'])
    cf.customNamedValues.addOrSetValue('hand', spec['hand'])
    cf.customNamedValues.addOrSetValue('flip', '1' if spec.get('flip') else '0')
    if var:
        cf.customNamedValues.addOrSetValue('blend', spec.get('blend', 'smooth'))
        cf.customNamedValues.addOrSetValue('stations', str(len(spec['stations'])))
        cf.customNamedValues.addOrSetValue('startType', spec.get('startType', 'natural'))
        cf.customNamedValues.addOrSetValue('endType', spec.get('endType', 'natural'))
    else:
        cf.customNamedValues.addOrSetValue('taperBy', spec.get('taperBy', 'angle'))
    return cf


def _feature_placement(inputs, comp):
    """(plane, centre, start, path) for a feature created outside a sketch.
    No plane picked: the centre point's or path's sketch plane, else XY."""
    plane, center_ent, start_ent, path_ent = (_picked(inputs, s) for s in ('plane', 'center', 'start', 'path'))
    if plane is None:
        sp = adsk.fusion.SketchPoint.cast(center_ent) if center_ent else None
        sc = adsk.fusion.SketchCurve.cast(path_ent) if path_ent else None
        owner = sp or sc
        plane = owner.parentSketch.referencePlane if owner else comp.xYConstructionPlane
    return plane, center_ent, start_ent, path_ent


class CreateExecute(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            inputs = adsk.core.CommandEventArgs.cast(args).command.commandInputs
            sk = _in_sketch()
            spec = _read_any_spec(inputs, sk)
            des = adsk.fusion.Design.cast(app.activeProduct)
            if sk:
                as_feature = adsk.core.BoolValueCommandInput.cast(inputs.itemById('asFeature')).value
                if not as_feature:
                    curve = sk.sketchCurves.sketchFixedSplines.addByNurbsCurve(build_curve(spec))
                    _store_spec_on_curve(curve, spec)
                    return
                # A feature can't carry the triad; it is placed by the plane, points and path only.
                _create_feature(sk.parentComponent, sk.referencePlane, spec,
                                _picked(inputs, 'center'), _picked(inputs, 'start'), _picked(inputs, 'path'))
                return
            comp = des.activeComponent
            plane, center_ent, start_ent, path_ent = _feature_placement(inputs, comp)
            _create_feature(comp, plane, spec, center_ent, start_ent, path_ent)
        except HelixError as e:
            ui.messageBox(str(e), 'Helix3D')
        except Exception:
            ui.messageBox('Helix3D create failed:\n' + traceback.format_exc())


class CreatePreview(adsk.core.CommandEventHandler):
    """Same geometry as execute, minus the custom feature (which must be added
    from execute). Fusion aborts it before the next preview or the execute."""
    def notify(self, args):
        try:
            args = adsk.core.CommandEventArgs.cast(args)
            inputs = args.command.commandInputs
            sk = _in_sketch()
            spec = _read_any_spec(inputs, sk)
            if sk:
                sk.sketchCurves.sketchFixedSplines.addByNurbsCurve(
                    build_curve(spec, PREVIEW_SAMPLES_PER_TURN))
                _show_highlight(spec, sk.parentComponent, sk.transform)
                return
            comp = adsk.fusion.Design.cast(app.activeProduct).activeComponent
            plane, center_ent, start_ent, path_ent = _feature_placement(inputs, comp)
            psk = _create_sketch(comp, plane, spec, center_ent, start_ent, path_ent,
                                 PREVIEW_SAMPLES_PER_TURN)
            _show_highlight(spec, comp, psk.transform)
        except HelixError:
            pass   # nothing to preview yet (no path picked)
        except Exception:
            _log('Helix3D preview failed:\n' + traceback.format_exc())


class CreateDestroy(adsk.core.CommandEventHandler):
    def notify(self, args):
        _stop_var_poll()
        _show_origin(False)
        try:
            _remember_placement_group(adsk.core.CommandEventArgs.cast(args).command.commandInputs)
        except Exception:
            pass


class CreateCreated(adsk.core.CommandCreatedEventHandler):
    def __init__(self, variable=False):
        super().__init__()
        self.variable = variable

    def notify(self, args):
        global _editing_deps, _touched, _touch_armed
        try:
            cmd = adsk.core.CommandCreatedEventArgs.cast(args).command
            _editing_deps, _touched, _touch_armed = {}, set(), True
            build = _add_var_inputs if self.variable else _add_inputs
            build(cmd.commandInputs, context='create', sketch=_in_sketch())
            if self.variable:
                _size_var_dialog(cmd)
                _start_var_poll(cmd)
            if not _in_sketch():
                _show_origin(True)
            _wire(cmd, CreateExecute(), TriadActivate() if _in_sketch() else None, CreateDestroy(),
                  CreatePreview())
        except Exception:
            ui.messageBox('Helix3D command failed:\n' + traceback.format_exc())


# --------------------------------------------------------------------------
# Edit feature command (Edit Feature on the timeline node)
# --------------------------------------------------------------------------
def _describe(ent):
    if ent is None:
        return 'none'
    kind = ent.objectType.split('::')[-1]
    if not ent.isValid:
        return kind + ' (invalid)'
    p = _point_world(ent)
    return kind + (' at (%.3f, %.3f, %.3f)' % (p.x, p.y, p.z) if p else '')


class EditExecute(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            inputs = adsk.core.CommandEventArgs.cast(args).command.commandInputs
            spec = _read_any_spec(inputs)
            cf = _editing
            # Grab the picks before the timeline moves; the inputs may not hold
            # them once their geometry is rolled away.
            picks = {sid: _effective_point(inputs, sid) for sid in ('center', 'start', 'path')}
            _log('Helix3D edit: picks ' + ', '.join('%s=%s' % (k, _describe(v)) for k, v in picks.items())
                 + '; touched=%s' % sorted(_touched))
            # Dependencies can only change while the marker sits just before the
            # feature; rolling back earlier (in activate) does not survive the
            # preview aborts and pan/orbit re-activations on this build.
            tl = _timeline()
            if not _roll_back_for_edit(cf):
                _log('Helix3D edit: marker %d, feature index %d after roll back'
                        % (tl.markerPosition, cf.timelineObject.index))
            for sid, label in (('center', 'Center Point'), ('start', 'Start Point'), ('path', 'Path')):
                new_ent = picks[sid]
                dep = cf.dependencies.itemById(sid)
                if new_ent is None:
                    if dep:
                        dep.deleteMe()
                    continue
                if not new_ent.isValid:
                    raise HelixError('%s: that geometry is created after the helix in the timeline, '
                                     'so it cannot drive the helix. Pick something earlier.' % label)
                if dep is None:
                    cf.dependencies.add(sid, new_ent)
                elif dep.entity.entityToken != new_ent.entityToken:
                    dep.entity = new_ent
            _log('Helix3D edit: dependencies now ' + ', '.join(
                '%s=%s' % (cf.dependencies.item(i).id, _describe(cf.dependencies.item(i).entity))
                for i in range(cf.dependencies.count)))
            cf.customNamedValues.addOrSetValue('hand', spec['hand'])
            cf.customNamedValues.addOrSetValue('flip', '1' if spec.get('flip') else '0')
            if spec['mode'] == MODE_VAR:
                cf.customNamedValues.addOrSetValue('blend', spec['blend'])
            for i in range(cf.parameters.count):
                p = cf.parameters.item(i)
                p.expression = spec['expr'][p.id]
            _restore_timeline()
            # Direction and points aren't parameters, so rebuild explicitly.
            _rebuild_feature(cf)
        except HelixError as e:
            ui.messageBox(str(e), 'Helix3D')
        except Exception:
            ui.messageBox('Helix3D edit failed:\n' + traceback.format_exc())


def _effective_point(inputs, sid):
    """The picker's entity; or, if the user never touched the picker, the point
    the feature already stores (the pickers are not reliably pre-filled)."""
    ent = _picked(inputs, sid)
    if ent is None and sid not in _touched:
        ent = _editing_deps.get(sid)
    return ent


class EditPreview(adsk.core.CommandEventHandler):
    """Hide the feature's own sketch and draw the would-be curve as custom
    graphics. Both live in the preview transaction, so they are redone on
    every change and dropped when the command ends."""
    def notify(self, args):
        try:
            inputs = adsk.core.CommandEventArgs.cast(args).command.commandInputs
            spec = _read_any_spec(inputs)
            center, start = _effective_point(inputs, 'center'), _effective_point(inputs, 'start')
            _apply_points(_model_to_sketch(_editing_xf), spec, center, start)
            _attach_path(spec, _editing_xf, _effective_point(inputs, 'path'))
            _log('Helix3D edit preview: picker center=%s start=%s; using center=%s start=%s radius=%.4f'
                 % (_describe(_picked(inputs, 'center')), _describe(_picked(inputs, 'start')),
                    _describe(center), _describe(start), spec['radius']))
            try:
                curve = build_curve(spec, PREVIEW_SAMPLES_PER_TURN)
            except HelixError:
                return
            sk = _sketch_of(_editing)
            if sk:
                sk.isVisible = False
            curve.transformBy(_editing_xf)
            _draw_preview(_editing.parentComponent, curve)
            _show_highlight(spec, _editing.parentComponent, _editing_xf)
        except Exception:
            _log('Helix3D edit preview failed:\n' + traceback.format_exc())


class EditActivate(adsk.core.CommandEventHandler):
    """First activation only: try to put the stored points into the selection
    inputs. The timeline is left alone here; execute rolls it back just for
    the dependency update."""
    def notify(self, args):
        global _pending_selections, _editing_activated, _touch_armed
        try:
            if _editing_activated:
                return
            _editing_activated = True
            args = adsk.core.CommandEventArgs.cast(args)
            # No command.beginStep() here: on this build it fires the execute
            # handler immediately (with the pickers still empty), which is what
            # used to wipe the stored points.
            _reselect_pending(args.command.commandInputs)
        except Exception:
            _log('Helix3D edit activate failed:\n' + traceback.format_exc())
        finally:
            _pending_selections = []
            _touch_armed = True


def _reselect_pending(inputs):
    for sid, ent in _pending_selections:
        sel = adsk.core.SelectionCommandInput.cast(inputs.itemById(sid))
        ok = sel.addSelection(ent) if sel else None
        _log('Helix3D activate: reselect %s (%s) -> %s, picker now holds %d'
             % (sid, _describe(ent), ok, sel.selectionCount if sel else -1))
    _apply_mode_visibility(inputs)   # radius/start angle hide when a start point is set
    _focus_first_empty_picker(inputs)


class EditPreSelect(adsk.core.SelectionEventHandler):
    """Only geometry that exists before the helix in the timeline can drive it."""
    def notify(self, args):
        try:
            args = adsk.core.SelectionEventArgs.cast(args)
            if args.activeInput.id not in ('center', 'start', 'path'):
                return
            ent = args.selection.entity
            sp = adsk.fusion.SketchPoint.cast(ent) or adsk.fusion.SketchCurve.cast(ent)
            cp = adsk.fusion.ConstructionPoint.cast(ent)
            tlo = sp.parentSketch.timelineObject if sp else (cp.timelineObject if cp else None)
            if tlo and tlo.index >= _editing.timelineObject.index:
                args.isSelectable = False
        except Exception:
            _log('Helix3D edit preselect failed:\n' + traceback.format_exc())


class EditDestroy(adsk.core.CommandEventHandler):
    def notify(self, args):
        global _editing, _editing_xf, _editing_restore
        _stop_var_poll()
        try:
            _restore_timeline()
            sk = _sketch_of(_editing)
            if sk:
                sk.isVisible = True
        except Exception:
            _log('Helix3D edit destroy failed:\n' + traceback.format_exc())
        _editing = _editing_xf = _editing_restore = None


class EditCreated(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        global _editing, _editing_xf, _editing_restore, _editing_rolled, _editing_activated
        global _editing_deps, _touched, _touch_armed
        try:
            cmd = adsk.core.CommandCreatedEventArgs.cast(args).command
            _editing = adsk.fusion.CustomFeature.cast(ui.activeSelections.item(0).entity)
            _editing_xf = _sketch_of(_editing).transform
            tl = _timeline()
            _editing_restore = tl.item(tl.markerPosition - 1) if tl.markerPosition > 0 else None
            _editing_rolled = False
            _editing_activated = False
            _touched = set()
            _touch_armed = False
            spec = _spec_of_feature(_editing)
            _editing_deps = {k: v for k, v in spec['_deps'].items() if v}
            _log('Helix3D edit open: stored ' + ', '.join('%s=%s' % (k, _describe(v)) for k, v in _editing_deps.items()))
            build = _add_var_inputs if spec['mode'] == MODE_VAR else _add_inputs
            build(cmd.commandInputs, spec, context='edit_feature')
            if spec['mode'] == MODE_VAR:
                _size_var_dialog(cmd)
                _start_var_poll(cmd)
            _wire(cmd, EditExecute(), EditActivate(), EditDestroy(), EditPreview())
            h = EditPreSelect()
            cmd.preSelect.add(h)
            _handlers.append(h)
        except Exception:
            ui.messageBox('Helix3D edit command failed:\n' + traceback.format_exc())


# --------------------------------------------------------------------------
# Edit sketch-helix command (marking menu on a selected helix curve)
# --------------------------------------------------------------------------
class SketchEditExecute(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            inputs = adsk.core.CommandEventArgs.cast(args).command.commandInputs
            spec = _read_any_spec(inputs, _editing_curve.parentSketch)
            _editing_curve.replaceGeometry(build_curve(spec))
            _store_spec_on_curve(_editing_curve, spec)
        except HelixError as e:
            ui.messageBox(str(e), 'Helix3D')
        except Exception:
            ui.messageBox('Helix3D sketch edit failed:\n' + traceback.format_exc())


class SketchEditPreview(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            args = adsk.core.CommandEventArgs.cast(args)
            spec = _read_any_spec(args.command.commandInputs, _editing_curve.parentSketch)
            # Coarse here, so dragging stays responsive; the execute handler
            # replaces this with the full rate curve when OK is pressed.
            _editing_curve.replaceGeometry(build_curve(spec, PREVIEW_SAMPLES_PER_TURN))
            psk = _editing_curve.parentSketch
            _show_highlight(spec, psk.parentComponent, psk.transform)
        except HelixError:
            pass
        except Exception:
            _log('Helix3D sketch edit preview failed:\n' + traceback.format_exc())


class SketchEditDestroy(adsk.core.CommandEventHandler):
    def notify(self, args):
        global _editing_curve
        _stop_var_poll()
        try:
            _remember_placement_group(adsk.core.CommandEventArgs.cast(args).command.commandInputs)
        except Exception:
            pass
        _editing_curve = None


class SketchEditActivate(adsk.core.CommandEventHandler):
    def notify(self, args):
        global _pending_selections, _editing_activated, _touch_armed
        try:
            if _editing_activated:
                return
            _editing_activated = True
            args = adsk.core.CommandEventArgs.cast(args)
            TriadActivate().notify(args)
            _reselect_pending(args.command.commandInputs)   # no beginStep(): it would execute now
        except Exception:
            _log('Helix3D sketch edit activate failed:\n' + traceback.format_exc())
        finally:
            _pending_selections = []
            _touch_armed = True


class SketchEditCreated(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        global _editing_curve, _editing_deps, _touched, _touch_armed, _editing_activated
        try:
            cmd = adsk.core.CommandCreatedEventArgs.cast(args).command
            curve = None
            for i in range(ui.activeSelections.count):
                e = ui.activeSelections.item(i).entity
                if _is_helix_curve(e):
                    curve = adsk.fusion.SketchFixedSpline.cast(e)
                    break
            if not curve:
                ui.messageBox('Select a helix curve created by Helix3D first.')
                return
            _editing_curve = curve
            spec = _spec_of_curve(curve)
            _editing_deps = {k: v for k, v in spec['_deps'].items() if v}
            _touched, _touch_armed, _editing_activated = set(), False, False
            build = _add_var_inputs if spec['mode'] == MODE_VAR else _add_inputs
            build(cmd.commandInputs, spec, context='edit_sketch', sketch=curve.parentSketch)
            if spec['mode'] == MODE_VAR:
                _size_var_dialog(cmd)
                _start_var_poll(cmd)
            _wire(cmd, SketchEditExecute(), SketchEditActivate(), SketchEditDestroy(), SketchEditPreview())
        except Exception:
            ui.messageBox('Helix3D sketch edit command failed:\n' + traceback.format_exc())


class CommandTerminated(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        try:
            cid = adsk.core.ApplicationCommandEventArgs.cast(args).commandId
            if cid in OUR_COMMANDS:
                # The user has just used the add-in, so the interface is up and
                # they are between jobs. That is when an update gets mentioned.
                _request_drain()
            if cid in OUR_COMMANDS or cid == 'SelectCommand':
                return
            # Pan/orbit terminate too; don't touch the model under one of our dialogs.
            if ui.activeCommand in OUR_COMMANDS:
                return
            _refresh_sketch_helices()
        except Exception:
            _log('Helix3D refresh failed:\n' + traceback.format_exc())


class MarkingMenu(adsk.core.MarkingMenuEventHandler):
    def notify(self, args):
        try:
            args = adsk.core.MarkingMenuEventArgs.cast(args)
            ents = args.selectedEntities
            if len(ents) != 1 or not _is_helix_curve(ents[0]):
                return
            menu = args.linearMarkingMenu.controls
            if not menu.itemById(SKETCH_EDIT_ID):
                menu.addSeparator(SKETCH_EDIT_ID + '_sep')
                menu.addCommand(ui.commandDefinitions.itemById(SKETCH_EDIT_ID))
        except Exception:
            _log('Helix3D marking menu failed:\n' + traceback.format_exc())


# --------------------------------------------------------------------------
# Updates: check GitHub for a new release, and install it when asked to
# --------------------------------------------------------------------------
GH_OWNER = 'RonnyM82'
GH_REPO = 'AutodeskFusionHelix3D'
GH_HOME = 'https://github.com/%s/%s' % (GH_OWNER, GH_REPO)
GH_LATEST = 'https://api.github.com/repos/%s/%s/releases/latest' % (GH_OWNER, GH_REPO)
GH_RELEASES = GH_HOME + '/releases'
ADDIN_ID = 'e38cea06-648e-4a3d-aac2-e6085495807c'
UPDATE_EVT = 'scottHelix3DUpdate'

API_TIMEOUT = 10        # per socket operation, not a total budget
DL_TIMEOUT = 60
API_DEADLINE = 30       # wall clock, because a slow drip never trips a socket timeout
DL_DEADLINE = 180
MIN_ZIP, MAX_ZIP = 10_000, 20_000_000
MAX_ENTRIES, MAX_UNPACKED = 200, 50_000_000
CHECK_INTERVAL = 24 * 3600
BACKOFF = (86400, 3 * 86400, 7 * 86400)   # by consecutive failure count
CLOCK_SANITY = 30 * 86400
OK_HOSTS = ('github.com', 'githubusercontent.com')

_update_ready = None    # (version, staged folder) waiting to be offered
_update_asked = False   # the question has been put once this session
_pending_confirmed = ['']   # a version installed earlier that a restart made live
_update_stop = None     # threading.Event that abandons an in-flight check
_update_gen = 0         # bumped in stop(), so a late event from an old load is ignored
_ssl_ctx = False        # False = not probed yet, None = probed, no trust anchors
_log_lock = threading.Lock()


class _UpdateError(Exception):
    """A failure worth reporting. `reason` is one line fit for a dialog;
    str(self) is the longer version that goes in the log."""
    def __init__(self, reason, detail=''):
        super().__init__(reason + ((': ' + detail) if detail else ''))
        self.reason = reason


def _ulog(msg):
    """Log from a background thread. File only: _log() calls app.log(), and
    Fusion's API is main-thread only."""
    try:
        with _log_lock:
            with open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(msg + '\n')
    except OSError:
        pass


# --------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------
def _parse_version(text):
    """'v0.8.0' -> (0, 8, 0, 0, 1). Anything unreadable -> None.

    Four numeric slots and a release flag. The flag is 0 when something
    followed the numbers, so 0.9.0-rc1 sorts below 0.9.0 without having to
    understand pre-release identifiers."""
    if not isinstance(text, str):
        return None
    s = text.strip().lstrip('vV').strip()
    core = re.split(r'[-+ _]', s, maxsplit=1)[0]
    parts = re.findall(r'\d+', core)
    if not parts:
        return None
    nums = [int(p) for p in parts[:4]]
    nums += [0] * (4 - len(nums))
    return tuple(nums) + (0 if len(s) > len(core) else 1,)


def _newer(a, b):
    """True when a is strictly newer than b. False if either side fails to
    parse, because a version nobody can read must never trigger an update."""
    pa, pb = _parse_version(a), _parse_version(b)
    return pa is not None and pb is not None and pa > pb


def _manifest_path():
    return os.path.join(_addin_dir(), 'Helix3D.manifest')


def _manifest_version(path=None):
    """The version string out of a manifest on disk, or '' if unreadable."""
    try:
        with open(path or _manifest_path(), encoding='utf-8') as f:
            return str(json.load(f).get('version', ''))
    except (OSError, ValueError, AttributeError):
        return ''


_VERSION = _manifest_version()   # captured before any install can change the file


def _restart_pending():
    """The version sitting on disk waiting for a Fusion restart, or ''.

    After an in-place install the manifest is newer than the module that read
    it at import. _newer rather than != so a manual downgrade does not read as
    a pending upgrade."""
    on_disk = _manifest_version()
    return on_disk if _newer(on_disk, _VERSION) else ''


# --------------------------------------------------------------------------
# Which kind of install is this
# --------------------------------------------------------------------------
def _stage_dir():
    return os.path.join(_addin_dir(), '.helix3d-update')


def _backup_dir():
    return os.path.join(_addin_dir(), '.helix3d-backup')


def _is_dev_install(folder=None):
    """True when this looks like the repo rather than an installed copy.

    Fusion can be pointed straight at a working tree, and overwriting one with
    a release zip would throw away uncommitted work. .git is tested for
    existence, not isdir, because a worktree or submodule checkout makes it a
    file holding 'gitdir: ...'. Both the literal and the resolved path are
    checked, in case the AddIns entry is a symlink or junction to the repo."""
    roots = [folder] if folder else [_addin_dir(), os.path.dirname(os.path.realpath(__file__))]
    for root in roots:
        for marker in ('.git', '.github', '.gitignore', 'tools'):
            if os.path.exists(os.path.join(root, marker)):
                return True
    return False


def _is_managed_install(folder=None):
    """True for an Autodesk App Store bundle, which the store's own updater
    owns. Self-updating one would fight it."""
    root = (folder or _addin_dir()).replace('\\', '/').lower().rstrip('/')
    return '/applicationplugins/' in root + '/' or root.endswith('.bundle')


def _may_install(folder=None):
    """(True, '') when installing here is allowed, else (False, reason)."""
    if _settings.get('update', {}).get('allowDevInstall'):
        return True, ''
    if _is_dev_install(folder):
        return False, ('Helix3D is running from its source repository, so it will not '
                       'replace its own files.')
    if _is_managed_install(folder):
        return False, ('Helix3D was installed from the Autodesk App Store, which handles '
                       'its own updates.')
    return True, ''


# --------------------------------------------------------------------------
# Settings: throttling, backoff, and what the user has already been told
# --------------------------------------------------------------------------
UPDATE_DEFAULTS = {
    'enabled': True,
    'nextCheck': 0.0,
    'lastCheck': 0.0,
    'failures': 0,
    'etag': '',
    'skipVersion': '',
    'pendingVersion': '',
    'pendingAt': 0.0,
    'broken': '',
}


def _upd():
    """The update section of the settings dict, created on first use.
    Main thread only: _save_settings rewrites the whole dict, so a worker
    writing here would race the dialog's own settings writes."""
    u = _settings.get('update')
    if not isinstance(u, dict):
        u = {}
        _settings['update'] = u
    for k, v in UPDATE_DEFAULTS.items():
        u.setdefault(k, v)
    return u


def _update_due(now=None):
    """True when a check is allowed to run.

    Wall clock, because the interval has to survive Fusion restarting. A
    nextCheck more than a month out means the clock jumped backwards at some
    point, and waiting for it would park the check for years."""
    now = now if now is not None else time.time()
    u = _upd()
    if not u.get('enabled', True):
        return False
    try:
        nxt = float(u.get('nextCheck') or 0)
    except (TypeError, ValueError):
        return True
    if nxt > now + CLOCK_SANITY:
        return True
    return now >= nxt


def _record_success(etag='', waiting=False):
    """Note that the check went through.

    waiting means a new version is sitting there unanswered, either because
    the user said Later or because they never opened a helix command. Holding
    the next check at now would make Fusion wait a full day before mentioning
    it again, so the daily interval only starts once there is nothing left to
    say."""
    u = _upd()
    u['lastCheck'] = time.time()
    u['nextCheck'] = 0.0 if waiting else time.time() + CHECK_INTERVAL
    u['failures'] = 0
    if etag:
        u['etag'] = etag
    _save_settings()


def _record_failure(reason, until=0.0):
    """Back off 1, then 3, then 7 days, so a site that permanently cannot
    reach GitHub is not retried every single day forever."""
    u = _upd()
    u['failures'] = int(u.get('failures') or 0) + 1
    wait = BACKOFF[min(u['failures'], len(BACKOFF)) - 1]
    u['lastCheck'] = time.time()
    u['nextCheck'] = max(until, time.time() + wait)
    _save_settings()
    _log('Helix3D update check failed (%s). Next try in %d days.' % (reason, wait // 86400))


def _should_offer(latest, skip=None, pending=None):
    """False when there is nothing worth putting in front of the user.

    The two versions can be passed in so the worker thread can ask the same
    question without reading the settings dict."""
    if not _newer(latest, _VERSION):
        return False
    if skip is None or pending is None:
        u = _upd()
        skip = u.get('skipVersion') or ''
        pending = u.get('pendingVersion') or ''
    # _newer rather than !=, so skipping 0.9.0 still surfaces 0.9.1.
    if skip and not _newer(latest, skip):
        return False
    return not (pending and not _newer(latest, pending))


def _clear_pending_if_restarted():
    """Called once from run(). When the version now running has caught up with
    the one that was downloaded, the restart happened. Returns the version to
    confirm to the user, once, or ''."""
    u = _upd()
    pending = u.get('pendingVersion') or ''
    if not pending:
        return ''
    if _newer(pending, _VERSION):
        return ''                     # still waiting for the restart
    u['pendingVersion'] = ''
    u['pendingAt'] = 0.0
    _save_settings()
    return pending if pending == _VERSION else ''


# --------------------------------------------------------------------------
# Talking to GitHub. All of this runs on the worker thread: no Fusion calls,
# no reads or writes of _settings.
# --------------------------------------------------------------------------
def _host_ok(url):
    """True only for https on GitHub's own hosts.

    hostname, never netloc: on https://github.com@evil.example/x the netloc is
    'github.com@evil.example' and a prefix test would wave it through.
    githubusercontent.com has to be here because a release asset always
    redirects onto release-assets.githubusercontent.com."""
    import urllib.parse
    p = urllib.parse.urlsplit(url)
    if p.scheme != 'https':
        return False
    host = (p.hostname or '').lower()
    return host in OK_HOSTS or host.endswith(tuple('.' + h for h in OK_HOSTS))


def _ssl_context():
    """A verifying SSL context, or None when no trust anchors can be found.

    Never returns an unverified context. A check that does not happen is the
    right answer when the certificate chain cannot be checked; silently
    trusting whatever answers is not.

    On Windows CPython loads the ROOT store and this is one line. Everywhere
    else it falls back to OpenSSL's compiled-in path, which Autodesk's build
    may or may not populate, hence the ladder."""
    global _ssl_ctx
    if _ssl_ctx is not False:
        return _ssl_ctx
    import ssl
    _ssl_ctx = None
    try:
        ctx = ssl.create_default_context()
    except Exception:
        _ulog('Helix3D: no SSL support, updates disabled.\n' + traceback.format_exc())
        return None
    if ctx.get_ca_certs():
        _ssl_ctx = ctx
        return ctx
    if sys.platform == 'darwin':
        for path in ('/etc/ssl/cert.pem', '/private/etc/ssl/cert.pem',
                     '/opt/homebrew/etc/openssl@3/cert.pem',
                     '/usr/local/etc/openssl@3/cert.pem'):
            try:
                ctx.load_verify_locations(cafile=path)
            except (OSError, ssl.SSLError):
                continue
            if ctx.get_ca_certs():
                _ssl_ctx = ctx
                return ctx
        try:
            pem = _macos_keychain_roots()
        except Exception:
            # Reading the keychain is the last rung, and a surprise there must
            # end in no update check, never in an exception out of a thread.
            _ulog('Helix3D: could not read the keychain:\n' + traceback.format_exc())
            pem = ''
        if pem:
            try:
                ctx.load_verify_locations(cadata=pem)
            except (ssl.SSLError, ValueError):
                pass
            if ctx.get_ca_certs():
                _ssl_ctx = ctx
                return ctx
    _ulog('Helix3D: no certificate authorities available, skipping the update check.')
    return None


def _macos_keychain_roots():
    """The system root certificates as PEM text, or ''.

    System.keychain matters as much as the Apple bundle: a company that runs a
    TLS-inspecting proxy puts its root there, and without it every request to
    GitHub fails to verify. Two honest limits. This exports every certificate
    in the keychain, including any the user marked Never Trust, and it does
    not evaluate trust settings at all. It is still a real trust store, which
    is the point: the ladder ends at giving up, never at trusting anything."""
    import subprocess
    out = []
    for kc in ('/System/Library/Keychains/SystemRootCertificates.keychain',
               '/Library/Keychains/System.keychain'):
        try:
            r = subprocess.run(['/usr/bin/security', 'find-certificate', '-a', '-p', kc],
                               capture_output=True, text=True, timeout=20,
                               stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0 and 'BEGIN CERTIFICATE' in (r.stdout or ''):
            out.append(r.stdout)
    return '\n'.join(out)


def _opener():
    """An opener that re-checks the host on every redirect.

    urllib follows redirects silently, so checking only the URL handed in
    would check the one hop that is never the one that matters. ProxyHandler
    with no arguments picks up the Windows and macOS system proxy settings.
    Never install_opener(): add-ins share one interpreter and that would
    change every other add-in's networking too."""
    import urllib.request
    ctx = _ssl_context()
    if ctx is None:
        raise _UpdateError('No certificate authorities are available to verify the connection.')

    class _SafeRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not _host_ok(newurl):
                raise _UpdateError('The download was redirected off GitHub.', newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    return urllib.request.build_opener(urllib.request.ProxyHandler(),
                                       urllib.request.HTTPSHandler(context=ctx),
                                       _SafeRedirect())


def _http_get(url, headers=None, timeout=API_TIMEOUT, deadline=API_DEADLINE,
              max_bytes=MAX_ZIP, min_bytes=0):
    """GET url, returning (body bytes, response headers).

    Read in chunks so the size cap and the wall clock are enforced against
    what actually arrives. Content-Length is a claim, not a fact, and the
    timeout on urlopen is per socket operation, so a slow drip would otherwise
    hang for as long as it liked."""
    import urllib.error
    import urllib.request
    if not _host_ok(url):
        raise _UpdateError('Refused to fetch an address that is not on GitHub.', url)
    hdrs = {'User-Agent': 'Helix3D/%s (+%s)' % (_VERSION or '0', GH_HOME),
            'Accept-Encoding': 'identity'}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, headers=hdrs)
    give_up = time.time() + deadline
    try:
        resp = _opener().open(req, timeout=timeout)
    except urllib.error.HTTPError:
        raise
    except _UpdateError:
        raise
    except Exception as e:
        raise _UpdateError('Could not reach GitHub.', '%s: %s' % (type(e).__name__, e))
    with resp:
        declared = resp.headers.get('Content-Length')
        if declared and declared.isdigit():
            n = int(declared)
            if n > max_bytes or (min_bytes and n < min_bytes):
                raise _UpdateError('The download was not the size it should be.',
                                   '%s bytes' % n)
        chunks, total = [], 0
        while True:
            if time.time() > give_up:
                raise _UpdateError('The download took too long.')
            if _update_stop is not None and _update_stop.is_set():
                raise _UpdateError('The update check was stopped.')
            try:
                chunk = resp.read(65536)
            except Exception as e:
                raise _UpdateError('The download was interrupted.',
                                   '%s: %s' % (type(e).__name__, e))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise _UpdateError('The download was larger than it should be.')
            chunks.append(chunk)
        return b''.join(chunks), resp.headers


def _fetch_latest(etag=''):
    """The latest release as a dict, or None when GitHub answers 304.

    Raises _UpdateError with `until` set when the rate limit is spent, so the
    caller can wait for the reset rather than backing off blindly. Measured
    against the live API: a 304 still costs one of the 60 requests an hour,
    whatever the documentation says, so the ETag saves bandwidth and nothing
    else."""
    import urllib.error
    headers = {'Accept': 'application/vnd.github+json',
               'X-GitHub-Api-Version': '2022-11-28'}
    if etag:
        headers['If-None-Match'] = etag
    try:
        body, resp_headers = _http_get(GH_LATEST, headers, max_bytes=2_000_000)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, etag
        if e.code in (403, 429) and (e.headers or {}).get('x-ratelimit-remaining') == '0':
            err = _UpdateError('GitHub is rate limiting this connection.', 'HTTP %d' % e.code)
            try:
                err.until = float(e.headers.get('x-ratelimit-reset', 0)) + 60
            except (TypeError, ValueError):
                err.until = time.time() + BACKOFF[0]
            raise err
        raise _UpdateError('GitHub answered with an error.', 'HTTP %d' % e.code)
    try:
        release = json.loads(body.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as e:
        raise _UpdateError('GitHub sent something that was not a release.', str(e))
    if not isinstance(release, dict) or not release.get('tag_name'):
        raise _UpdateError('GitHub sent a release with no tag.')
    return release, resp_headers.get('ETag', '') or etag


def _pick_asset(release, tag):
    """(download url, declared size) for this release's zip.

    A workflow run that half failed leaves a release with no asset on it, so
    this is a real case rather than defensive padding."""
    want = 'Helix3D-%s.zip' % tag.lstrip('vV')
    for asset in release.get('assets') or []:
        if not isinstance(asset, dict) or asset.get('name') != want:
            continue
        url = asset.get('browser_download_url') or ''
        size = asset.get('size')
        if not _host_ok(url):
            raise _UpdateError('The release links its download somewhere other than GitHub.', url)
        if not isinstance(size, int) or not MIN_ZIP <= size <= MAX_ZIP:
            raise _UpdateError('The release download is not a plausible size.', repr(size))
        return url, size
    raise _UpdateError('That release has no %s to download.' % want)


def _download(url, expect_size):
    """The release zip as bytes.

    Three size checks catching three different things: Content-Length rejects
    an obviously wrong response before a byte is read, the running cap catches
    a server that lied about it, and matching the API's own byte count exactly
    catches truncation. That last one is the strong one, because the number
    came down a separate connection."""
    body, _ = _http_get(url, timeout=DL_TIMEOUT, deadline=DL_DEADLINE,
                        max_bytes=MAX_ZIP, min_bytes=MIN_ZIP)
    if len(body) != expect_size:
        raise _UpdateError('The download did not arrive in one piece.',
                           'got %d bytes, expected %d' % (len(body), expect_size))
    return body


# --------------------------------------------------------------------------
# Unpacking a release, and checking it is what it claims to be
# --------------------------------------------------------------------------
def _safe_member(root_real, name):
    """The absolute destination for a zip entry, or None if it must be
    refused.

    A backslash is worth its own test: 'a\\..\\..\\b' is one harmless
    component on a Mac and a way out of the folder on Windows. realpath rather
    than abspath because an earlier entry could have left a symlink for a
    later one to write through."""
    if not name or name.endswith('/'):
        return None
    if '\\' in name or name.startswith('/') or ':' in name:
        return None
    parts = name.split('/')
    if any(p in ('', '.', '..') for p in parts):
        return None
    dest = os.path.realpath(os.path.join(root_real, *parts))
    try:
        if os.path.commonpath([root_real, dest]) != root_real:
            return None
    except ValueError:      # different drives on Windows
        return None
    return dest


def _stage_release(blob, tag, into):
    """Unpack the zip into `into` and check it thoroughly. Returns the path of
    the unpacked Helix3D folder. Worker thread: no Fusion, no settings."""
    import io
    version = tag.lstrip('vV')
    shutil.rmtree(into, ignore_errors=True)
    os.makedirs(into, exist_ok=True)
    root_real = os.path.realpath(into)
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as e:
        raise _UpdateError('The download was not a usable zip file.', str(e))
    with zf:
        infos = zf.infolist()
        # Both of these are checked before anything is written, which is the
        # only moment a zip bomb can still be refused for free.
        if len(infos) > MAX_ENTRIES:
            raise _UpdateError('The download holds far more files than a release should.')
        if sum(i.file_size for i in infos) > MAX_UNPACKED:
            raise _UpdateError('The download unpacks to far more than a release should.')
        tops = {n.split('/')[0] for n in zf.namelist() if n.strip()}
        if tops != {'Helix3D'}:
            raise _UpdateError('The download is not laid out like a Helix3D release.',
                               'top level: %s' % sorted(tops))
        names = set(zf.namelist())
        for required in ('Helix3D/Helix3D.py', 'Helix3D/Helix3D.manifest'):
            if required not in names:
                raise _UpdateError('The download is missing %s.' % required.split('/')[-1])
        for info in infos:
            if stat.S_ISLNK(info.external_attr >> 16):
                raise _UpdateError('The download contains a link where a file should be.',
                                   info.filename)
            if info.filename.endswith('/'):
                continue
            dest = _safe_member(root_real, info.filename)
            if dest is None:
                raise _UpdateError('The download tried to write outside its own folder.',
                                   info.filename)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(info) as src, open(dest, 'wb') as out:
                shutil.copyfileobj(src, out)
        try:
            source = zf.read('Helix3D/Helix3D.py').decode('utf-8')
        except (UnicodeDecodeError, zipfile.BadZipFile) as e:
            raise _UpdateError('The new Helix3D.py could not be read.', str(e))
    # This proves the file parses, not that it works. The release workflow
    # already compiles it, so this is really a check on the journey here.
    try:
        compile(source, 'Helix3D.py', 'exec')
    except SyntaxError as e:
        raise _UpdateError('The new Helix3D.py did not arrive intact.', str(e))
    staged = os.path.join(into, 'Helix3D')
    try:
        with open(os.path.join(staged, 'Helix3D.manifest'), encoding='utf-8') as f:
            manifest = json.load(f)
    except (OSError, ValueError) as e:
        raise _UpdateError('The new manifest could not be read.', str(e))
    if str(manifest.get('version', '')) != version:
        raise _UpdateError('The release is tagged %s but ships version %s.'
                           % (version, manifest.get('version')))
    if str(manifest.get('id', '')) != ADDIN_ID:
        raise _UpdateError('The download is a different add-in, not Helix3D.')
    if str(manifest.get('type', '')) != 'addin':
        raise _UpdateError('The download is not an add-in.')
    return staged


# --------------------------------------------------------------------------
# Putting the new files in place. Main thread: this serialises with the
# settings writes, and nothing should read a half-copied resources folder.
# --------------------------------------------------------------------------
def _plan_files(staged):
    """[(source, relative destination)] in the order they get installed.

    Inert files first, then Helix3D.py, then the manifest last. The manifest
    version is what says which version is installed, so until it moves the
    folder still honestly describes the old one and every recovery path knows
    where it stands."""
    last = ('Helix3D.py', 'Helix3D.manifest')
    plan = []
    for root, dirs, files in os.walk(staged):
        dirs.sort()
        for name in sorted(files):
            src = os.path.join(root, name)
            rel = os.path.relpath(src, staged).replace('\\', '/')
            if rel not in last:
                plan.append((src, rel))
    for rel in last:
        src = os.path.join(staged, rel)
        if os.path.isfile(src):
            plan.append((src, rel))
    return plan


def _replace_file(src, dst, attempts=5):
    """Move src onto dst, retrying a few times.

    On a Mac this cannot fail for the reason people expect: rename unlinks the
    old entry and anything still reading the old file keeps its own copy of
    it. On Windows a PermissionError here means something else has the file
    open, usually an antivirus scanner or the search indexer, and it normally
    clears inside a second."""
    wait = 0.2
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise _UpdateError('Another program is holding %s open.'
                                   % os.path.basename(dst))
            time.sleep(wait)
            wait *= 2
        except OSError as e:
            raise _UpdateError('Could not write %s.' % os.path.basename(dst), str(e))


def _probe_writable(plan, folder):
    """Open every file the install will replace and close it again.

    Cheap and slightly racy, but it catches the whole class of failures worth
    catching before anything has been touched: an add-in folder that needs
    elevation, or a file something has pinned."""
    for _, rel in plan:
        dst = os.path.join(folder, rel.replace('/', os.sep))
        if not os.path.isfile(dst):
            continue
        try:
            with open(dst, 'r+b'):
                pass
        except OSError as e:
            raise _UpdateError('Helix3D cannot write to its own folder, so it cannot '
                               'update itself. Install the new version by hand.',
                               '%s: %s' % (dst, e))


def _purge_pycache(folder):
    """Best effort. CPython invalidates a .pyc when the source mtime or size
    stops matching, so a leftover one will not actually be used. Removing it
    anyway costs a line and saves an afternoon of wondering why the old code
    is still running."""
    try:
        shutil.rmtree(os.path.join(folder, '__pycache__'), ignore_errors=True)
    except Exception:
        pass


def _backup(plan, folder, backup):
    """Copy every file the install will replace into the backup folder."""
    shutil.rmtree(backup, ignore_errors=True)
    os.makedirs(backup, exist_ok=True)
    saved = []
    for _, rel in plan:
        dst = os.path.join(folder, rel.replace('/', os.sep))
        if not os.path.isfile(dst):
            continue
        keep = os.path.join(backup, rel.replace('/', os.sep))
        os.makedirs(os.path.dirname(keep), exist_ok=True)
        try:
            shutil.copy2(dst, keep)
        except OSError as e:
            raise _UpdateError('Could not back up %s before replacing it.'
                               % os.path.basename(dst), str(e))
        saved.append(rel)
    return saved


def _rollback(saved, folder, backup):
    """Put the backed-up files back, newest change first. Best effort by
    nature: if a move failed going forward it can fail coming back. Returns
    True only when everything was restored."""
    whole = True
    for rel in reversed(saved):
        keep = os.path.join(backup, rel.replace('/', os.sep))
        dst = os.path.join(folder, rel.replace('/', os.sep))
        try:
            shutil.copy2(keep, dst)
        except OSError:
            whole = False
            _log('Helix3D could not restore %s:\n%s' % (rel, traceback.format_exc()))
    return whole


def _take_lock(stage):
    """A lock file, so two Fusion windows cannot install at once. One older
    than ten minutes is left over from a process that died."""
    os.makedirs(stage, exist_ok=True)
    path = os.path.join(stage, 'lock')
    try:
        if os.path.isfile(path) and time.time() - os.path.getmtime(path) > 600:
            os.remove(path)
    except OSError:
        pass
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise _UpdateError('Another Fusion window is already installing this update.')
    except OSError as e:
        raise _UpdateError('Could not start the install.', str(e))
    os.close(fd)
    return path


def _release_lock(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _install(staged, folder=None):
    """Copy the staged release over the add-in folder.

    Helix3D.settings.json and Helix3D.log are safe without doing anything:
    they are not in the zip, so they are not in the plan, and nothing here
    deletes. That is the main reason this copies file by file instead of
    swapping the whole folder, which on Windows can fail outright when the
    folder holds the running module."""
    folder = folder or _addin_dir()
    allowed, why = _may_install(folder)
    if not allowed:
        raise _UpdateError(why)
    plan = _plan_files(staged)
    if not plan:
        raise _UpdateError('There was nothing to install.')
    backup = _backup_dir()
    lock = _take_lock(_stage_dir())
    try:
        _probe_writable(plan, folder)
        saved = _backup(plan, folder, backup)
        done = []
        try:
            for src, rel in plan:
                if rel == 'Helix3D.py':
                    _purge_pycache(folder)
                dst = os.path.join(folder, rel.replace('/', os.sep))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                tmp = dst + '.helix3d-tmp'
                shutil.copy2(src, tmp)
                _replace_file(tmp, dst)
                done.append(rel)
        except Exception:
            whole = _rollback(saved, folder, backup)
            if not whole:
                _upd()['broken'] = _VERSION
                _save_settings()
            raise
        return len(done)
    finally:
        _release_lock(lock)


def _clear_stage():
    """Drop a staging folder left behind by a download that Fusion killed on
    the way out. The backup folder is deliberately kept: it is the manual way
    back to the previous version, and it is replaced at the start of the next
    install rather than deleted at the end of this one."""
    try:
        shutil.rmtree(_stage_dir(), ignore_errors=True)
    except Exception:
        pass


# --------------------------------------------------------------------------
# The worker, and the bridge back to the main thread
# --------------------------------------------------------------------------
def _update_worker(etag, skip, pending, may_install, stop, gen):
    """Ask GitHub, and download the new release if there is one.

    Everything it needs out of the settings dict was read on the main thread
    and handed over, because _save_settings rewrites the whole dict and two
    threads writing it would lose one of them. It reports back by firing a
    custom event with a small JSON payload rather than sharing a global."""
    out = {'gen': gen}
    try:
        release, new_etag = _fetch_latest(etag)
        out['etag'] = new_etag
        if release is None:
            out['state'] = 'current'          # 304, nothing has changed
        else:
            tag = str(release.get('tag_name') or '')
            version = tag.lstrip('vV')
            out['version'] = version
            if not _should_offer(version, skip, pending):
                out['state'] = 'current'
            elif not may_install:
                # Running from the repo. Worth knowing about, but nothing gets
                # downloaded, because staging would litter the working tree.
                out['state'] = 'noinstall'
            else:
                url, size = _pick_asset(release, tag)
                if stop.is_set():
                    return
                out['staged'] = _stage_release(_download(url, size), tag, _stage_dir())
                out['state'] = 'ready'
    except _UpdateError as e:
        out['state'] = 'failed'
        out['reason'] = e.reason
        out['until'] = getattr(e, 'until', 0.0)
        _ulog('Helix3D update check: ' + str(e))
    except Exception:
        out['state'] = 'failed'
        out['reason'] = 'Something went wrong while checking for updates.'
        _ulog('Helix3D update check failed:\n' + traceback.format_exc())
    if stop.is_set():
        return
    try:
        app.fireCustomEvent(UPDATE_EVT, json.dumps(out))
    except Exception:
        pass


class UpdateEvent(adsk.core.CustomEventHandler):
    """Runs on the main thread, which is the only place the settings dict and
    the Fusion API may be touched. It records what the worker found and stops
    there; the question itself waits until the user has actually used the
    add-in, so a message box never lands on top of Fusion still starting up."""
    def notify(self, args):
        global _update_ready
        try:
            out = json.loads(args.additionalInfo or '{}')
            if out.get('gen') != _update_gen:
                return                       # left over from a previous load
            state = out.get('state')
            if state == 'drain':
                _drain_update()
                return
            if state == 'failed':
                _record_failure(out.get('reason') or 'unknown', out.get('until') or 0.0)
                return
            _record_success(out.get('etag') or '', waiting=(state == 'ready'))
            version = out.get('version') or ''
            if state == 'noinstall':
                _log('Helix3D %s has been released. This copy runs from its source '
                     'repository, so pull it rather than updating in place.' % version)
            elif state == 'ready':
                # Only a session-long global, deliberately. pendingVersion in
                # the settings file means "installed, waiting for a restart",
                # and nothing else, so that a download the user postpones does
                # not look like one they accepted.
                _update_ready = (version, out.get('staged') or '')
                _ulog('Helix3D %s downloaded and waiting to be offered.' % version)
        except Exception:
            _log('Helix3D update handler failed:\n' + traceback.format_exc())


def _start_update_check(force=False):
    """Main thread. Start the daily check on a daemon thread.

    Nothing here blocks Fusion starting: the thread does the waiting, and the
    answer comes back through the custom event."""
    global _update_stop
    try:
        if not force and not _update_due():
            return
        allowed, _why = _may_install()
        u = _upd()
        stop = threading.Event()
        _update_stop = stop
        args = (u.get('etag') or '', u.get('skipVersion') or '',
                u.get('pendingVersion') or '', allowed, stop, _update_gen)
        threading.Thread(target=_update_worker, args=args, daemon=True).start()
    except Exception:
        _log('Helix3D could not start the update check:\n' + traceback.format_exc())


def _stop_update_check():
    global _update_stop, _update_gen
    if _update_stop is not None:
        _update_stop.set()
    _update_stop = None
    _update_gen += 1        # any event still in flight belongs to the old load


# --------------------------------------------------------------------------
# Asking the user
# --------------------------------------------------------------------------
def _ui_is_idle():
    """True when no command is running, so a message box cannot land on top of
    an open dialog."""
    try:
        return ui.activeCommand in ('', 'SelectCommand')
    except Exception:
        return False


def _request_drain():
    """Ask for the question to be put, once a Helix3D command has finished.

    It goes through the custom event rather than being asked on the spot,
    because commandTerminated fires while Fusion is still winding the command
    down and a modal box blocks the very loop that has to finish doing it. By
    the time the event is delivered the command is properly gone."""
    if _update_asked or not (_update_ready or _pending_confirmed[0]):
        return
    try:
        app.fireCustomEvent(UPDATE_EVT, json.dumps({'gen': _update_gen, 'state': 'drain'}))
    except Exception:
        pass


def _drain_update():
    """Put the question, having waited for a Helix3D command to finish, which
    is proof the interface is alive and the user has reached for the tool.
    Offers the download once per session, and confirms an update that a
    restart has now made live."""
    global _update_ready, _update_asked
    if _update_asked or not _ui_is_idle():
        return
    done = _pending_confirmed[0]
    if done:
        _pending_confirmed[0] = ''
        _update_asked = True
        ui.messageBox('Helix3D is now running version %s.' % done, 'Helix3D')
        return
    if not _update_ready:
        return
    version, staged = _update_ready
    _update_asked = True
    answer = ui.messageBox(
        'Helix3D %s is available. You are running %s.\n\n'
        'Yes: install it now.\n'
        'No: not now, ask again next time Fusion starts.\n'
        'Cancel: skip this version for good.' % (version, _VERSION),
        'Helix3D update',
        adsk.core.MessageBoxButtonTypes.YesNoCancelButtonType,
        adsk.core.MessageBoxIconTypes.QuestionIconType)
    if answer == adsk.core.DialogResults.DialogCancel:
        u = _upd()
        u['skipVersion'] = version
        _save_settings()
        _update_ready = None
        _clear_stage()
        _log('Helix3D %s skipped at the user\'s request.' % version)
        return
    if answer != adsk.core.DialogResults.DialogYes:
        # Later. The staging folder goes too, so the next session downloads
        # again rather than trusting files nobody has looked at since. It is
        # thirty kilobytes.
        _update_ready = None
        _clear_stage()
        return
    try:
        _install(staged)
    except _UpdateError as e:
        _log('Helix3D %s failed to install: %s' % (version, e))
        _update_ready = None
        _clear_stage()
        ui.messageBox('%s\n\nYou can download it yourself from\n%s'
                      % (e.reason, GH_RELEASES), 'Helix3D update')
        return
    except Exception:
        _log('Helix3D %s failed to install:\n%s' % (version, traceback.format_exc()))
        _update_ready = None
        _clear_stage()
        ui.messageBox('Helix3D could not install the update. You can download it '
                      'yourself from\n%s' % GH_RELEASES, 'Helix3D update')
        return
    u = _upd()
    u['pendingVersion'] = version
    u['pendingAt'] = time.time()
    _save_settings()
    _update_ready = None
    _clear_stage()
    _log('Helix3D %s installed, waiting for a restart.' % version)
    # Restart Fusion, never "restart the add-in": Fusion only reads a manifest
    # at startup, and a half-loaded add-in fails in ways that look like a bug.
    ui.messageBox('Helix3D %s is installed.\n\nRestart Fusion to start using it.'
                  % version, 'Helix3D update')


def _update_startup():
    """Called from run(). Tidies up after the previous session and starts a
    check if one is due. Nothing here shows any interface."""
    global _update_ready, _update_asked
    try:
        _update_ready, _update_asked = None, False
        _clear_stage()
        _pending_confirmed[0] = _clear_pending_if_restarted()
        broken = _upd().get('broken') or ''
        if broken:
            _upd()['broken'] = ''
            _save_settings()
            _log('Helix3D could not fully undo a failed update. If the commands '
                 'misbehave, reinstall from ' + GH_RELEASES)
        waiting = _restart_pending()
        if waiting:
            _log('Helix3D %s is installed on disk. Restart Fusion to start using it.'
                 % waiting)
            return
        _start_update_check()
    except Exception:
        _log('Helix3D update startup failed:\n' + traceback.format_exc())


# --------------------------------------------------------------------------
# Add-in entry points
# --------------------------------------------------------------------------
def _button(cid, name, tip, handler, icons=ICONS):
    old = ui.commandDefinitions.itemById(cid)
    if old:
        old.deleteMe()
    d = ui.commandDefinitions.addButtonDefinition(cid, name, tip, icons)
    d.commandCreated.add(handler)
    _handlers.append(handler)
    return d


def run(context):
    global _def, _def_var
    try:
        create_def = _button(CMD_ID, '3D Helix', 'Create a 3D helix or spiral sketch curve', CreateCreated())
        _button(EDIT_ID, 'Edit 3D Helix', 'Edit a parametric 3D helix', EditCreated())
        _button(SKETCH_EDIT_ID, 'Edit 3D Helix', 'Edit this helix curve', SketchEditCreated())
        var_def = _button(VAR_CMD_ID, 'Variable Pitch Helix',
                          'Create a helix whose pitch and radius change along its length, '
                          'for progressive springs and timing screws',
                          CreateCreated(variable=True), VAR_ICONS)
        var_def.toolClipFilename = _clip('stations.png')
        _button(VAR_EDIT_ID, 'Edit Variable Pitch Helix',
                'Edit a parametric variable pitch helix', EditCreated(), VAR_ICONS)

        # The edit command must exist before it is assigned to the definition.
        _def = adsk.fusion.CustomFeatureDefinition.create(CF_ID, 'Helix', ICONS)
        _def.editCommandId = EDIT_ID
        h = ComputeHandler()
        _def.customFeatureCompute.add(h)
        _handlers.append(h)

        _def_var = adsk.fusion.CustomFeatureDefinition.create(VAR_CF_ID, 'Variable Helix', VAR_ICONS)
        _def_var.editCommandId = VAR_EDIT_ID
        h = ComputeHandler()
        _def_var.customFeatureCompute.add(h)
        _handlers.append(h)

        h = MarkingMenu()
        ui.markingMenuDisplaying.add(h)
        _handlers.append(h)

        app.unregisterCustomEvent(TOUCH_EVT)   # stale registration from a previous run
        h = DeferredTouch()
        app.registerCustomEvent(TOUCH_EVT).add(h)
        _handlers.append(h)

        app.unregisterCustomEvent(VAR_POLL_EVT)
        h = VarPoll()
        app.registerCustomEvent(VAR_POLL_EVT).add(h)
        _handlers.append(h)

        app.unregisterCustomEvent(UPDATE_EVT)
        h = UpdateEvent()
        app.registerCustomEvent(UPDATE_EVT).add(h)
        _handlers.append(h)

        h = CommandTerminated()
        ui.commandTerminated.add(h)
        _handlers.append(h)

        for pid in PANELS:
            panel = ui.allToolbarPanels.itemById(pid)
            if not panel:
                continue
            for cid, cdef in ((CMD_ID, create_def), (VAR_CMD_ID, var_def)):
                if not panel.controls.itemById(cid):
                    ctrl = panel.controls.addCommand(cdef)
                    ctrl.isPromoted = True

        # Last, and silent. It starts a thread and returns; nothing is shown
        # until the user has finished with a helix command.
        _update_startup()
    except Exception:
        ui.messageBox('Helix3D failed to start:\n' + traceback.format_exc())


def stop(context):
    try:
        _stop_var_poll()
        _stop_update_check()
        app.unregisterCustomEvent(TOUCH_EVT)
        app.unregisterCustomEvent(VAR_POLL_EVT)
        app.unregisterCustomEvent(UPDATE_EVT)
        for pid in PANELS:
            panel = ui.allToolbarPanels.itemById(pid)
            for cid in (CMD_ID, VAR_CMD_ID):
                ctrl = panel.controls.itemById(cid) if panel else None
                if ctrl:
                    ctrl.deleteMe()
        for cid in OUR_COMMANDS:
            d = ui.commandDefinitions.itemById(cid)
            if d:
                d.deleteMe()
    except Exception:
        ui.messageBox('Helix3D failed to stop:\n' + traceback.format_exc())
