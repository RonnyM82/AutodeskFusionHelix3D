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
import traceback

app = adsk.core.Application.get()
ui = app.userInterface

CF_ID = 'moyseHelix3D_v2'
CMD_ID = 'scottHelix3DCreate'
EDIT_ID = 'scottHelix3DEdit'
SKETCH_EDIT_ID = 'scottHelix3DSketchEdit'
PANELS = ('SketchCreatePanel', 'SolidCreatePanel')
ICONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources')
ATTR_GROUP = 'Helix3D'
SAMPLES_PER_TURN = 24

MODES = ['Revolutions & Pitch', 'Revolutions & Height', 'Height & Pitch', 'Spiral (flat)']
MODE_RP, MODE_RH, MODE_HP, MODE_SPIRAL = MODES

_handlers = []
_def = None
_editing = None         # CustomFeature in the feature edit dialog
_editing_xf = None      # its sketch's transform (sketch -> model), read before rolling back
_editing_restore = None # TimelineObject the marker sat after when the edit dialog opened
_editing_rolled = False
_editing_activated = False  # activate re-fires after every pan/orbit; only the first one sets up
_editing_deps = {}      # {'center': entity, 'start': entity} as stored when the dialog opened
_touched = set()        # point inputs the user changed (or cleared) in the edit dialog
_touch_armed = False    # ignore inputChanged until the dialog has finished setting itself up
TOUCH_EVT = 'scottHelix3DDeferredTouch'
_touch_pending = set()  # entity tokens of features rebuilt by compute since the last touch
_touching = False
_editing_curve = None   # SketchFixedSpline in the sketch edit dialog
_origin_restore = None  # (component, previous lightbulb state)
_pending_triad = None   # Matrix3D to push onto the triad when the dialog activates
_pending_selections = []  # [(input id, entity)] to select once the dialog activates
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Helix3D.log')
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Helix3D.settings.json')
_triad_last = None      # last meaningful triad transform (world); restored when the triad is re-shown


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


def helix_nurbs(r0, dr, height, turns, start_ang, right_hand, per_turn=SAMPLES_PER_TURN):
    """Control points and knots for a cubic B-spline helix around Z, centred on
    the origin, starting at z=0. Radius varies linearly from r0 to r0+dr."""
    n = max(6, int(math.ceil(abs(turns) * per_turn)))
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

    Q = [pt(k / n) for k in range(n + 1)]
    D0, Dn = deriv(0.0), deriv(1.0)
    uk = [k / n for k in range(n + 1)]
    p, m = 3, n + 3
    U = [0.0] * 4 + uk[1:n] + [1.0] * 4
    P = [None] * m
    P[0], P[m - 1] = Q[0], Q[n]
    P[1] = tuple(Q[0][c] + U[4] / 3.0 * D0[c] for c in range(3))
    P[m - 2] = tuple(Q[n][c] - (1 - U[m - 1]) / 3.0 * Dn[c] for c in range(3))

    nu = n - 1
    A = [[0.0] * nu for _ in range(nu)]
    rhs = [[0.0] * 3 for _ in range(nu)]
    for r in range(nu):
        u = uk[r + 1]
        span = _find_span(m - 1, p, u, U)
        N = _basis_funs(span, u, p, U)
        for j in range(p + 1):
            idx = span - p + j
            if idx <= 1 or idx >= m - 2:
                for c in range(3):
                    rhs[r][c] -= N[j] * P[idx][c]
            else:
                A[r][idx - 2] += N[j]
        for c in range(3):
            rhs[r][c] += Q[r + 1][c]
    for i in range(nu):
        piv = max(range(i, nu), key=lambda k: abs(A[k][i]))
        A[i], A[piv] = A[piv], A[i]
        rhs[i], rhs[piv] = rhs[piv], rhs[i]
        for k in range(i + 1, nu):
            f = A[k][i] / A[i][i]
            if f == 0:
                continue
            for j in range(i, nu):
                A[k][j] -= f * A[i][j]
            for c in range(3):
                rhs[k][c] -= f * rhs[i][c]
    X = [[0.0] * 3 for _ in range(nu)]
    for i in range(nu - 1, -1, -1):
        for c in range(3):
            s = rhs[i][c] - sum(A[i][j] * X[j][c] for j in range(i + 1, nu))
            X[i][c] = s / A[i][i]
    for i in range(nu):
        P[i + 2] = tuple(X[i])
    return P, U


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
    else:  # spiral
        turns, height = spec['turns'], 0.0
    if mode == MODE_SPIRAL:
        dr = spec['endRadius'] - r0
    else:
        dr = math.tan(spec.get('taper', 0.0)) * height
    return r0, dr, height, turns


def build_curve(spec):
    """NurbsCurve3D in sketch space from a spec dict."""
    r0, dr, height, turns = resolve(spec)
    P, U = helix_nurbs(r0, dr, height, turns, spec['startAngle'], spec['hand'] == 'right')
    pts = [adsk.core.Point3D.create(*p) for p in P]
    xf = spec.get('xform')
    if xf:
        M = adsk.core.Matrix3D.create()
        M.setWithArray(xf)
        for pt in pts:
            pt.transformBy(M)
    return adsk.core.NurbsCurve3D.createNonRational(pts, 3, U, False)


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
    global _pending_selections
    _pending_selections = []
    lu = app.activeProduct.unitsManager.defaultLengthUnits
    spec = spec or {}
    v = lambda k, d: adsk.core.ValueInput.createByReal(spec.get(k, d))

    dd = inputs.addDropDownCommandInput('mode', 'Mode', adsk.core.DropDownStyles.TextListDropDownStyle)
    cur = spec.get('mode', MODE_RP)
    for mname in MODES:
        dd.listItems.add(mname, mname == cur)
    dd.isEnabled = context != 'edit_feature'  # can't add/remove params on an existing feature

    if context == 'create' and not _in_sketch():
        sel = inputs.addSelectionInput('plane', 'Plane', 'Sketch plane. Helix axis is the plane normal. Empty = XY, or the centre point\'s sketch plane.')
        sel.addSelectionFilter('ConstructionPlanes')
        sel.addSelectionFilter('PlanarFaces')
        sel.setSelectionLimits(0, 1)
    for sid, label, tip in (('center', 'Center Point', 'Optional. The helix axis passes through this point.'),
                            ('start', 'Start Point', 'Optional. Where the helix starts: sets radius, start angle and axial offset.')):
        sel = inputs.addSelectionInput(sid, label, tip)
        for flt in ('SketchPoints', 'ConstructionPoints', 'Vertices'):
            sel.addSelectionFilter(flt)
        sel.setSelectionLimits(0, 1)
        ent = spec.get('_deps', {}).get(sid)
        if ent:
            # Selections can't be set during commandCreated; do it in activate.
            _pending_selections.append((sid, ent))
    if context == 'create' and _in_sketch():
        inputs.addBoolValueInput('asFeature', 'Finish sketch and create parametric feature', True, '', False)

    inputs.addValueInput('radius', 'Radius', lu, v('radius', 2.0))
    inputs.addValueInput('endRadius', 'End Radius', lu, v('endRadius', 4.0))
    inputs.addValueInput('pitch', 'Pitch', lu, v('pitch', 1.0))
    inputs.addValueInput('height', 'Height', lu, v('height', 3.0))
    inputs.addValueInput('turns', 'Revolutions', '', v('turns', 3.0))
    inputs.addValueInput('taper', 'Taper Angle', 'deg', v('taper', 0.0))
    inputs.addValueInput('startAngle', 'Start Angle', 'deg', v('startAngle', 0.0))
    hd = inputs.addDropDownCommandInput('hand', 'Direction', adsk.core.DropDownStyles.TextListDropDownStyle)
    rh = spec.get('hand', 'right') == 'right'
    hd.listItems.add('Right hand', rh)
    hd.listItems.add('Left hand', not rh)

    if sketch is not None:  # in-sketch create or edit: placement triad
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
    _apply_mode_visibility(inputs)


def _apply_mode_visibility(inputs):
    mode = adsk.core.DropDownCommandInput.cast(inputs.itemById('mode')).selectedItem.name
    show = {
        MODE_RP: ('pitch', 'turns', 'taper'),
        MODE_RH: ('turns', 'height', 'taper'),
        MODE_HP: ('height', 'pitch', 'taper'),
        MODE_SPIRAL: ('endRadius', 'turns'),
    }[mode]
    for k in ('endRadius', 'pitch', 'height', 'turns', 'taper'):
        inputs.itemById(k).isVisible = k in show
    if inputs.itemById('start'):
        has_start = _effective_point(inputs, 'start') is not None
        has_center = _effective_point(inputs, 'center') is not None
        inputs.itemById('radius').isVisible = not has_start
        inputs.itemById('startAngle').isVisible = not has_start
        grp = inputs.itemById('placement_grp')
        if grp:
            pinned = has_center and has_start   # both points: axis = sketch normal, triad has no say
            if grp.isVisible == pinned:
                grp.isVisible = not pinned
                tri = adsk.core.TriadCommandInput.cast(inputs.itemById('placement'))
                if not pinned and tri and _triad_last:
                    tri.transform = _triad_last   # showing the triad again resets it


def _read_spec(inputs, sketch=None):
    """Spec dict with internal-unit values plus the expressions typed.
    sketch: needed to convert the triad transform into sketch space."""
    val = lambda k: adsk.core.ValueCommandInput.cast(inputs.itemById(k))
    spec = {
        'mode': adsk.core.DropDownCommandInput.cast(inputs.itemById('mode')).selectedItem.name,
        'hand': 'right' if adsk.core.DropDownCommandInput.cast(
            inputs.itemById('hand')).selectedItem.name.startswith('Right') else 'left',
        'expr': {},
    }
    for k in ('radius', 'endRadius', 'pitch', 'height', 'turns', 'taper', 'startAngle'):
        spec[k] = val(k).value
        spec['expr'][k] = val(k).expression
    tri = inputs.itemById('placement')
    if tri and sketch is not None:
        global _triad_last
        tri = adsk.core.TriadCommandInput.cast(tri)
        axis = None
        for label, key in AXIS_CHOICES:
            if adsk.core.DropDownCommandInput.cast(inputs.itemById('axis')).selectedItem.name == label:
                axis = key
        # positionTransform reports identity on current builds; transform is fine as scaling is hidden.
        # A hidden triad has been reset, so keep using the last one we saw.
        if inputs.itemById('placement_grp').isVisible:
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
    return spec


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


def _param_ids(mode):
    return {
        MODE_RP: ('radius', 'pitch', 'turns', 'taper', 'startAngle'),
        MODE_RH: ('radius', 'turns', 'height', 'taper', 'startAngle'),
        MODE_HP: ('radius', 'height', 'pitch', 'taper', 'startAngle'),
        MODE_SPIRAL: ('radius', 'endRadius', 'turns', 'startAngle'),
    }[mode]


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


PARAM_META = {  # id: (display name, unit key)
    'radius': ('Radius', 'len'), 'endRadius': ('End Radius', 'len'),
    'pitch': ('Pitch', 'len'), 'height': ('Height', 'len'),
    'turns': ('Revolutions', ''), 'taper': ('Taper Angle', 'deg'),
    'startAngle': ('Start Angle', 'deg'),
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
            spec['radius'] = r
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
            '_deps': {k: _dep_entity(cf, k) for k in ('center', 'start')}}
    for i in range(cf.parameters.count):
        p = cf.parameters.item(i)
        spec[p.id] = p.value
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
    spec['_deps'] = {k: _entity_by_token(spec.get(k + 'Token')) for k in ('center', 'start')}
    return spec


def _is_helix_curve(entity):
    try:
        return bool(entity.attributes.itemByName(ATTR_GROUP, 'spec'))
    except Exception:
        return False


UNIT_OF = {'radius': 'len', 'endRadius': 'len', 'pitch': 'len', 'height': 'len',
           'turns': '', 'taper': 'deg', 'startAngle': 'deg'}


def _evaluate_spec(spec, sketch):
    """Re-evaluate the stored expressions against the current user parameters
    and the placement against the current centre / start points. Returns True
    if anything changed (spec is updated in place)."""
    um = app.activeProduct.unitsManager
    lu = um.defaultLengthUnits
    changed = False
    for k, expr in spec.get('expr', {}).items():
        unit = {'len': lu, 'deg': 'deg', '': ''}[UNIT_OF[k]]
        try:
            v = um.evaluateExpression(expr, unit)
        except Exception:
            continue  # a referenced parameter went away; keep the last value
        if abs(v - spec.get(k, v)) > 1e-9:
            spec[k] = v
            changed = True
    if spec.get('triad'):
        before = (spec.get('xform'), spec['radius'], spec['startAngle'])
        _place_in_sketch(spec, sketch, _entity_by_token(spec.get('centerToken')),
                         _entity_by_token(spec.get('startToken')))
        if before != (spec.get('xform'), spec['radius'], spec['startAngle']):
            changed = True
    return changed


def _refresh_sketch_helices():
    """Called after any command finishes: update in-sketch helices whose
    expressions or driving points now give a different curve."""
    des = adsk.fusion.Design.cast(app.activeProduct)
    if not des:
        return
    for attr in des.findAttributes(ATTR_GROUP, 'spec'):
        curve = adsk.fusion.SketchFixedSpline.cast(attr.parent)
        if not curve or not curve.isValid:
            continue
        spec = json.loads(attr.value)
        if _evaluate_spec(spec, curve.parentSketch):
            curve.replaceGeometry(build_curve(spec))
            attr.value = json.dumps(spec)


def _store_spec_on_curve(curve, spec):
    curve.attributes.add(ATTR_GROUP, 'spec', json.dumps({k: v for k, v in spec.items() if not k.startswith('_')}))


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
            if ui.activeCommand in (CMD_ID, EDIT_ID, SKETCH_EDIT_ID):
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
            tri = adsk.core.TriadCommandInput.cast(inputs.itemById('placement'))
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
            if _touch_armed and args.input.id in ('center', 'start'):
                _touched.add(args.input.id)
            if args.input.id == 'center':
                _triad_follow_center(args.inputs)
            if args.input.id in ('mode', 'center', 'start'):
                _apply_mode_visibility(args.inputs)
            _advance_focus(args.inputs, args.input.id)
        except Exception:
            _log('Helix3D inputChanged failed:\n' + traceback.format_exc())


def _picked(inputs, sid):
    sel = adsk.core.SelectionCommandInput.cast(inputs.itemById(sid))
    return sel.selection(0).entity if sel and sel.selectionCount else None


NEXT_PICKER = {'plane': 'center', 'center': 'start', 'start': None}


def _advance_focus(inputs, changed_id):
    """Each picker takes one entity, so once it is filled hand selection focus
    to the next picker. After the last one Fusion is left to its own devices:
    the API can't give a value box focus, and releasing picker focus just
    sends it to the canvas."""
    if changed_id not in NEXT_PICKER:
        return
    sel = adsk.core.SelectionCommandInput.cast(inputs.itemById(changed_id))
    if not sel or sel.selectionCount == 0:
        return
    nxt = inputs.itemById(NEXT_PICKER[changed_id]) if NEXT_PICKER[changed_id] else None
    if nxt and nxt.isVisible:
        adsk.core.SelectionCommandInput.cast(nxt).hasFocus = True


def _triad_follow_center(inputs):
    """Sit the triad on the centre point so its position isn't misleading."""
    global _triad_last
    tri = adsk.core.TriadCommandInput.cast(inputs.itemById('placement'))
    c = _effective_point(inputs, 'center')
    if not tri or c is None:
        return
    T = tri.transform.copy()
    p = _point_world(c)
    T.translation = adsk.core.Vector3D.create(p.x, p.y, p.z)
    tri.transform = T
    _triad_last = T


def _remember_placement_group(inputs):
    grp = inputs.itemById('placement_grp')
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
def _create_sketch(comp, plane, spec, center_ent=None, start_ent=None):
    sk = comp.sketches.add(plane)
    sk.name = 'Helix'
    _apply_points(_model_to_sketch(sk.transform), spec, center_ent, start_ent)
    sk.sketchCurves.sketchFixedSplines.addByNurbsCurve(build_curve(spec))
    return sk


def _create_feature(comp, plane, spec, center_ent=None, start_ent=None):
    lu = app.activeProduct.unitsManager.defaultLengthUnits
    units = {'len': lu, 'deg': 'deg', '': ''}
    sk = _create_sketch(comp, plane, spec, center_ent, start_ent)

    cfi = comp.features.customFeatures.createInput(_def)
    for pid in _param_ids(spec['mode']):
        name, ukey = PARAM_META[pid]
        cfi.addCustomParameter(pid, name, adsk.core.ValueInput.createByString(spec['expr'][pid]),
                               units[ukey], True)
    cfi.addDependency('plane', plane)
    if center_ent:
        cfi.addDependency('center', center_ent)
    if start_ent:
        cfi.addDependency('start', start_ent)
    cfi.setStartAndEndFeatures(sk, sk)
    cf = comp.features.customFeatures.add(cfi)
    cf.customNamedValues.addOrSetValue('mode', spec['mode'])
    cf.customNamedValues.addOrSetValue('hand', spec['hand'])
    return cf


def _feature_placement(inputs, comp):
    """(plane, centre point, start point) for a feature created outside a sketch."""
    plane, center_ent, start_ent = _picked(inputs, 'plane'), _picked(inputs, 'center'), _picked(inputs, 'start')
    if plane is None:
        sp = adsk.fusion.SketchPoint.cast(center_ent) if center_ent else None
        plane = sp.parentSketch.referencePlane if sp else comp.xYConstructionPlane
    return plane, center_ent, start_ent


class CreateExecute(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            inputs = adsk.core.CommandEventArgs.cast(args).command.commandInputs
            sk = _in_sketch()
            spec = _read_spec(inputs, sk)
            des = adsk.fusion.Design.cast(app.activeProduct)
            if sk:
                as_feature = adsk.core.BoolValueCommandInput.cast(inputs.itemById('asFeature')).value
                if not as_feature:
                    curve = sk.sketchCurves.sketchFixedSplines.addByNurbsCurve(build_curve(spec))
                    _store_spec_on_curve(curve, spec)
                    return
                # A feature can't carry the triad; it is placed by the plane and points only.
                _create_feature(sk.parentComponent, sk.referencePlane, spec,
                                _picked(inputs, 'center'), _picked(inputs, 'start'))
                return
            comp = des.activeComponent
            plane, center_ent, start_ent = _feature_placement(inputs, comp)
            _create_feature(comp, plane, spec, center_ent, start_ent)
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
            spec = _read_spec(inputs, sk)
            if sk:
                curve = sk.sketchCurves.sketchFixedSplines.addByNurbsCurve(build_curve(spec))
                if not adsk.core.BoolValueCommandInput.cast(inputs.itemById('asFeature')).value:
                    _store_spec_on_curve(curve, spec)
                    args.isValidResult = True
                return
            comp = adsk.fusion.Design.cast(app.activeProduct).activeComponent
            plane, center_ent, start_ent = _feature_placement(inputs, comp)
            _create_sketch(comp, plane, spec, center_ent, start_ent)
        except Exception:
            _log('Helix3D preview failed:\n' + traceback.format_exc())


class CreateDestroy(adsk.core.CommandEventHandler):
    def notify(self, args):
        _show_origin(False)
        try:
            _remember_placement_group(adsk.core.CommandEventArgs.cast(args).command.commandInputs)
        except Exception:
            pass


class CreateCreated(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        global _editing_deps, _touched, _touch_armed
        try:
            cmd = adsk.core.CommandCreatedEventArgs.cast(args).command
            _editing_deps, _touched, _touch_armed = {}, set(), True
            _add_inputs(cmd.commandInputs, context='create', sketch=_in_sketch())
            if not _in_sketch():
                _show_origin(True)
            _wire(cmd, CreateExecute(), TriadActivate() if _in_sketch() else None, CreateDestroy(),
                  CreatePreview())
        except Exception:
            ui.messageBox('Helix3D command failed:\n' + traceback.format_exc())


# --------------------------------------------------------------------------
# Edit feature command (Edit Feature on the timeline node)
# --------------------------------------------------------------------------
class HelixError(Exception):
    """A message for the user, not a traceback."""


def _describe(ent):
    if ent is None:
        return 'none'
    p = _point_world(ent) if ent.isValid else None
    return '%s%s' % (ent.objectType.split('::')[-1],
                     ' at (%.3f, %.3f, %.3f)' % (p.x, p.y, p.z) if p else ' (invalid)')


class EditExecute(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            inputs = adsk.core.CommandEventArgs.cast(args).command.commandInputs
            spec = _read_spec(inputs)
            cf = _editing
            # Grab the picks before the timeline moves; the inputs may not hold
            # them once their geometry is rolled away.
            picks = {sid: _effective_point(inputs, sid) for sid in ('center', 'start')}
            _log('Helix3D edit: picks ' + ', '.join('%s=%s' % (k, _describe(v)) for k, v in picks.items())
                 + '; touched=%s' % sorted(_touched))
            # Dependencies can only change while the marker sits just before the
            # feature; rolling back earlier (in activate) does not survive the
            # preview aborts and pan/orbit re-activations on this build.
            tl = _timeline()
            if not _roll_back_for_edit(cf):
                _log('Helix3D edit: marker %d, feature index %d after roll back'
                        % (tl.markerPosition, cf.timelineObject.index))
            for sid, label in (('center', 'Center Point'), ('start', 'Start Point')):
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
            spec = _read_spec(inputs)
            center, start = _effective_point(inputs, 'center'), _effective_point(inputs, 'start')
            _apply_points(_model_to_sketch(_editing_xf), spec, center, start)
            _log('Helix3D edit preview: picker center=%s start=%s; using center=%s start=%s radius=%.4f'
                 % (_describe(_picked(inputs, 'center')), _describe(_picked(inputs, 'start')),
                    _describe(center), _describe(start), spec['radius']))
            sk = _sketch_of(_editing)
            if sk:
                sk.isVisible = False
            curve = build_curve(spec)
            curve.transformBy(_editing_xf)
            _draw_preview(_editing.parentComponent, curve)
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


class EditPreSelect(adsk.core.SelectionEventHandler):
    """Only geometry that exists before the helix in the timeline can drive it."""
    def notify(self, args):
        try:
            args = adsk.core.SelectionEventArgs.cast(args)
            if args.activeInput.id not in ('center', 'start'):
                return
            ent = args.selection.entity
            sp = adsk.fusion.SketchPoint.cast(ent)
            cp = adsk.fusion.ConstructionPoint.cast(ent)
            tlo = sp.parentSketch.timelineObject if sp else (cp.timelineObject if cp else None)
            if tlo and tlo.index >= _editing.timelineObject.index:
                args.isSelectable = False
        except Exception:
            _log('Helix3D edit preselect failed:\n' + traceback.format_exc())


class EditDestroy(adsk.core.CommandEventHandler):
    def notify(self, args):
        global _editing, _editing_xf, _editing_restore
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
            _add_inputs(cmd.commandInputs, spec, context='edit_feature')
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
            spec = _read_spec(inputs, _editing_curve.parentSketch)
            _editing_curve.replaceGeometry(build_curve(spec))
            _store_spec_on_curve(_editing_curve, spec)
        except Exception:
            ui.messageBox('Helix3D sketch edit failed:\n' + traceback.format_exc())


class SketchEditPreview(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            args = adsk.core.CommandEventArgs.cast(args)
            spec = _read_spec(args.command.commandInputs, _editing_curve.parentSketch)
            _editing_curve.replaceGeometry(build_curve(spec))
            _store_spec_on_curve(_editing_curve, spec)
            args.isValidResult = True   # preview is the final result; execute is skipped
        except Exception:
            _log('Helix3D sketch edit preview failed:\n' + traceback.format_exc())


class SketchEditDestroy(adsk.core.CommandEventHandler):
    def notify(self, args):
        global _editing_curve
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
            _add_inputs(cmd.commandInputs, spec, context='edit_sketch', sketch=curve.parentSketch)
            _wire(cmd, SketchEditExecute(), SketchEditActivate(), SketchEditDestroy(), SketchEditPreview())
        except Exception:
            ui.messageBox('Helix3D sketch edit command failed:\n' + traceback.format_exc())


class CommandTerminated(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        try:
            cid = adsk.core.ApplicationCommandEventArgs.cast(args).commandId
            if cid in (CMD_ID, EDIT_ID, SKETCH_EDIT_ID, 'SelectCommand'):
                return
            # Pan/orbit terminate too; don't touch the model under one of our dialogs.
            if ui.activeCommand in (CMD_ID, EDIT_ID, SKETCH_EDIT_ID):
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
def _button(cid, name, tip, handler):
    old = ui.commandDefinitions.itemById(cid)
    if old:
        old.deleteMe()
    d = ui.commandDefinitions.addButtonDefinition(cid, name, tip, ICONS)
    d.commandCreated.add(handler)
    _handlers.append(handler)
    return d


def run(context):
    global _def
    try:
        create_def = _button(CMD_ID, '3D Helix', 'Create a 3D helix or spiral sketch curve', CreateCreated())
        _button(EDIT_ID, 'Edit 3D Helix', 'Edit a parametric 3D helix', EditCreated())
        _button(SKETCH_EDIT_ID, 'Edit 3D Helix', 'Edit this helix curve', SketchEditCreated())

        # The edit command must exist before it is assigned to the definition.
        _def = adsk.fusion.CustomFeatureDefinition.create(CF_ID, 'Helix', ICONS)
        _def.editCommandId = EDIT_ID
        h = ComputeHandler()
        _def.customFeatureCompute.add(h)
        _handlers.append(h)

        h = MarkingMenu()
        ui.markingMenuDisplaying.add(h)
        _handlers.append(h)

        app.unregisterCustomEvent(TOUCH_EVT)   # stale registration from a previous run
        h = DeferredTouch()
        app.registerCustomEvent(TOUCH_EVT).add(h)
        _handlers.append(h)

        h = CommandTerminated()
        ui.commandTerminated.add(h)
        _handlers.append(h)

        for pid in PANELS:
            panel = ui.allToolbarPanels.itemById(pid)
            if panel and not panel.controls.itemById(CMD_ID):
                ctrl = panel.controls.addCommand(create_def)
                ctrl.isPromoted = True
    except Exception:
        ui.messageBox('Helix3D failed to start:\n' + traceback.format_exc())


def stop(context):
    try:
        app.unregisterCustomEvent(TOUCH_EVT)
        for pid in PANELS:
            panel = ui.allToolbarPanels.itemById(pid)
            ctrl = panel.controls.itemById(CMD_ID) if panel else None
            if ctrl:
                ctrl.deleteMe()
        for cid in (CMD_ID, EDIT_ID, SKETCH_EDIT_ID):
            d = ui.commandDefinitions.itemById(cid)
            if d:
                d.deleteMe()
    except Exception:
        ui.messageBox('Helix3D failed to stop:\n' + traceback.format_exc())
