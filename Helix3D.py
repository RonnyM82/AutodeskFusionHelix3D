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
import threading
import traceback

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
    fl = inputs.addBoolValueInput('flip', 'Flip direction', True, '', bool(spec.get('flip')))
    fl.tooltip = ('Run the helix the other way: down the axis instead of up, or from the far '
                  'end of the path back. The winding stays right or left handed either way.')

    _add_placement(inputs, spec, sketch)
    _apply_mode_visibility(inputs)


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
    pos.tooltip = 'Where the helix sits. Pick nothing and it starts at the origin of the sketch plane.'
    _add_pickers(pos.children, spec, context, with_path=False,
                 start_tip='Optional. Where the helix starts: sets the start angle and the '
                           'height it starts at. The radii come from the table.')
    _add_placement(pos.children, spec, sketch)

    grp = inputs.addGroupCommandInput('stations_grp', 'Stations')
    grp.isExpanded = True
    grp.tooltip = ('Station 1 is where the helix starts. Each row after it says how many '
                   'turns on from the row above it sits, and the pitch and radius there. '
                   'Click into a row and a cross marks its station on the preview, the run '
                   'its turns value measures lights up, and a note says what that row is '
                   'doing to the helix.')
    ch = grp.children
    cnt = ch.addIntegerSpinnerCommandInput('stations', 'Stations', 2, MAX_STATIONS, 1, n)
    cnt.isEnabled = context != 'edit_feature'   # a feature's parameters are fixed
    cnt.tooltip = ('How many points along the helix you set a pitch and a radius at. '
                   'Fusion fixes which parameters a custom feature owns when it is '
                   'created, so this is greyed out when you edit one.')
    bl = ch.addDropDownCommandInput('blend', 'Blend', adsk.core.DropDownStyles.TextListDropDownStyle)
    cur_blend = spec.get('blend', 'smooth')
    for label, key in BLENDS:
        bl.listItems.add(label, key == cur_blend)
    bl.tooltip = ('How the pitch gets from one station to the next. Smooth keeps the '
                  'curvature continuous, so a sweep along it has no crease at a station. '
                  'Linear ramps in a straight line, which is what SOLIDWORKS does.')

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
    ends.tooltip = ('Natural starts or finishes at the station as it stands. Flat adds a run '
                    'at the pitch you give, for the flat turns, then eases into the station '
                    'over the transition turns. Zero pitch is a true flat, which is what a '
                    'timing screw dwell wants. A closed spring end wants the wire diameter, '
                    'or the sweep passes through itself.')
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
    fl.tooltip = ('Run the helix down the axis instead of up. The winding stays right or '
                  'left handed either way, and the stations stay in the order you typed.')

    if context == 'create' and _in_sketch():
        inputs.addBoolValueInput('asFeature', 'Finish sketch and create parametric feature', True, '', False)

    _apply_var_visibility(inputs)
    _update_var_readout(inputs)


def _add_station_row(table, k, turns, pitch, radius, lu):
    """Row k of the table, station k. The seeds are ValueInputs; turns is
    ignored for row 1, which is the start and has nothing before it."""
    tc = table.commandInputs
    table.addCommandInput(tc.addTextBoxCommandInput('s%d' % k, '', '<b>%d</b>' % k, 1, True), k, 0)
    if k == 1:
        table.addCommandInput(tc.addTextBoxCommandInput('t_start', '', 'start', 1, True), k, 1)
    else:
        table.addCommandInput(tc.addValueInput('turns%d' % (k - 1), 'Turns', '', turns), k, 1)
    table.addCommandInput(tc.addValueInput('pitch%d' % k, 'Pitch', lu, pitch), k, 2)
    table.addCommandInput(tc.addValueInput('radius%d' % k, 'Radius', lu, radius), k, 3)


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
        return xs[o]
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


def _point_at(spec, u, out=0.0):
    """_helix_point with the helix's own placement applied. `out` pushes the
    point that much further from the axis, to sit a label clear of the coil."""
    p = _helix_point(spec, u)
    if out:
        r = math.hypot(p.x, p.y)
        if r > 1e-9:
            k = 1.0 + out / r
            p = adsk.core.Point3D.create(p.x * k, p.y * k, p.z)
    xf = spec.get('xform')
    if xf:
        M = adsk.core.Matrix3D.create()
        M.setWithArray(xf)
        p.transformBy(M)
    return p


def _focus_size(spec):
    """Marker and text sized off the helix, so they stay in proportion."""
    r = max([st.get('radius', 0.0) for st in spec['stations']] or [1.0])
    return max(r * 0.30, 0.25), max(r * 0.13, 0.12)     # cross arm, text height


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
    length = lambda v: um.formatInternalValue(v, lu, True)
    turns = lambda v: um.formatInternalValue(v, '', False)
    st = spec['stations'][k - 1]
    rng = _var_focus_range(spec)
    if rng:
        sts = _expanded_stations(spec)
        xs, ps, _ = _station_axes(sts)
        ramp = _Ramp(xs, ps, spec.get('blend', 'smooth') == 'smooth')
        rise = ramp.integral(rng[1]) - ramp.integral(rng[0])
        head = '%s turns on, rising %s' % (turns(rng[1] - rng[0]), length(rise))
        u = (rng[0] + rng[1]) / 2.0
    else:
        head = 'where the helix starts'
        u = _var_focus_station(spec)
    # Three short lines read better on a billboard than two long ones.
    text = 'Station %d\n%s\npitch %s, radius %s' % (
        k, head, length(st.get('pitch', 0.0)), length(st.get('radius', 0.0)))
    r = max([t.get('radius', 0.0) for t in spec['stations']] or [1.0])
    return text, _point_at(spec, u, r * 0.45), _focus_size(spec)[1]


ORANGE = (255, 140, 0)


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
    paint = adsk.fusion.CustomGraphicsSolidColorEffect.create(adsk.core.Color.create(*(ORANGE + (255,))))

    def draw(geom, weight):
        e = g.addCurve(geom)
        if not e:
            return
        e.weight = weight
        e.color = paint
        try:
            e.depthPriority = 1     # over the curve it sits on
        except Exception:
            pass

    if curve is not None:
        curve.transformBy(xf)
        draw(curve, 5)
    if mark is not None:
        p, size = mark
        p.transformBy(xf)
        for dx, dy, dz in ((size, 0, 0), (0, size, 0), (0, 0, size)):
            draw(adsk.core.Line3D.create(
                adsk.core.Point3D.create(p.x - dx, p.y - dy, p.z - dz),
                adsk.core.Point3D.create(p.x + dx, p.y + dy, p.z + dz)), 3)
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
                    _var_focus = ('start',)
                elif cid.startswith('end'):
                    _var_focus = ('end',)
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
    except Exception:
        ui.messageBox('Helix3D failed to start:\n' + traceback.format_exc())


def stop(context):
    try:
        _stop_var_poll()
        app.unregisterCustomEvent(TOUCH_EVT)
        app.unregisterCustomEvent(VAR_POLL_EVT)
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
