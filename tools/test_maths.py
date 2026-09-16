"""Checks on the variable pitch maths: the height a station table comes to, and
the pitch solved for one run when the height is pinned instead.

None of it touches Fusion. _Ramp, _station_axes, var_helix_height and
solve_group_pitch are plain Python over plain dicts, so the whole thing runs
under a stub in place of adsk:

    python tools/test_maths.py
"""

import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILED = []


def load_addin():
    """Import Helix3D with a stub in place of adsk. The same trick
    tools/test_updates.py uses, kept here so either file runs on its own."""
    for name in ('adsk', 'adsk.core', 'adsk.fusion'):
        sys.modules.setdefault(name, types.ModuleType(name))
    core, fusion = sys.modules['adsk.core'], sys.modules['adsk.fusion']
    sys.modules['adsk'].core, sys.modules['adsk'].fusion = core, fusion

    class _App(object):
        userInterface = None
        activeProduct = None

        def log(self, msg):
            pass

        def registerCustomEvent(self, name):
            return type('E', (), {'add': staticmethod(lambda h: None)})

        def unregisterCustomEvent(self, name):
            pass

        def fireCustomEvent(self, name, info=''):
            pass

    core.Application = type('A', (), {'get': staticmethod(_App)})
    core.__getattr__ = lambda name: object
    fusion.__getattr__ = lambda name: object
    sys.path.insert(0, ROOT)
    import Helix3D
    return Helix3D


H = load_addin()


def ok(label, got, want=True):
    if got != want:
        FAILED.append(label)
        print('  FAIL %s: got %r, wanted %r' % (label, got, want))
    else:
        print('  ok   %s' % label)


def close(label, got, want, tol=1e-9):
    if abs(got - want) > tol:
        FAILED.append(label)
        print('  FAIL %s: got %.12f, wanted %.12f' % (label, got, want))
    else:
        print('  ok   %s (%.6f)' % (label, got))


def refuses(label, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except H.HelixError as e:
        print('  ok   %s (%s)' % (label, str(e)[:55]))
        return
    except Exception as e:
        FAILED.append(label)
        print('  FAIL %s: raised %s: %s' % (label, type(e).__name__, e))
        return
    FAILED.append(label)
    print('  FAIL %s: was accepted' % label)


def spring(pitch2=1.0, pitch3=1.0):
    """The dialog's own starting table: a closed end, the working coils, then a
    closed end again."""
    return [{'turns': 1.0, 'pitch': 0.2, 'radius': 2.0},
            {'turns': 3.0, 'pitch': pitch2, 'radius': 2.0},
            {'turns': 1.0, 'pitch': pitch3, 'radius': 2.0},
            {'pitch': 0.2, 'radius': 2.0}]


FLAT = [{'turns': 5.0, 'pitch': 2.0, 'radius': 1.0}, {'pitch': 2.0, 'radius': 1.0}]

print('')
print('Height from the table')
# Two stations at one pitch: the pitch curve is flat, so the height is pitch
# times turns exactly, under either blend.
close('a flat run is pitch times turns', H.var_helix_height(FLAT, True), 10.0)
close('and the same when it ramps in straight lines', H.var_helix_height(FLAT, False), 10.0)
# A single span between two stations covers the average of the two pitches.
ramp = [{'turns': 4.0, 'pitch': 1.0, 'radius': 1.0}, {'pitch': 3.0, 'radius': 1.0}]
close('one ramp rises by the average pitch', H.var_helix_height(ramp, False), 8.0)
close('smooth covers the same area on a single span', H.var_helix_height(ramp, True), 8.0)

refuses('a table of one station', H.var_helix_height, [{'pitch': 1.0, 'radius': 1.0}])
refuses('a negative pitch', H.var_helix_height,
        [{'turns': 1.0, 'pitch': -1.0, 'radius': 1.0}, {'pitch': 1.0, 'radius': 1.0}])
refuses('a radius of zero', H.var_helix_height,
        [{'turns': 1.0, 'pitch': 1.0, 'radius': 0.0}, {'pitch': 1.0, 'radius': 1.0}])
refuses('no turns between two stations', H.var_helix_height,
        [{'turns': 0.0, 'pitch': 1.0, 'radius': 1.0}, {'pitch': 1.0, 'radius': 1.0}])

print('')
print('How far a start point stands off the axis')
# Straight up Z through the origin: the radius is just the distance out in XY.
close('a point beside a vertical axis',
      H._off_axis((3.0, 4.0, 17.0), (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)), 5.0)
close('sliding it along the axis changes nothing',
      H._off_axis((3.0, 4.0, -900.0), (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)), 5.0)
close('a point on the axis has no radius',
      H._off_axis((0.0, 0.0, 6.0), (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)), 0.0)
# An axis that is neither through the origin nor along a triad direction.
d = H._unit((1.0, 2.0, 2.0))          # length 3, so the unit vector is exact
close('an angled axis somewhere else',
      H._off_axis((5.0, 1.0, 1.0), (5.0, 1.0, 1.0), d), 0.0)
close('and a point three out from it',
      H._off_axis((5.0 + 3.0 * 2.0 / 3.0, 1.0 - 3.0 * 2.0 / 3.0, 1.0 + 3.0 * 1.0 / 3.0),
                  (5.0, 1.0, 1.0), d), 3.0)

print('')
print('Solving one run for a pinned height')
# Both stations of a flat run solved together, so the answer is arithmetic.
close('a flat run solves to height over turns',
      H.solve_group_pitch(FLAT, [0, 1], 10.0, True), 2.0)
close('and follows the height up',
      H.solve_group_pitch(FLAT, [0, 1], 15.0, True), 3.0)

for smooth in (True, False):
    name = 'smooth' if smooth else 'linear'
    sts = spring()
    p = H.solve_group_pitch(sts, [1, 2], 6.0, smooth)
    solved = H._with_group_pitch(sts, [1, 2], p)
    close('%s: the solved table comes out at the height' % name,
          H.var_helix_height(solved, smooth), 6.0, 1e-7)
    # Both members land on one pitch, so the run between them is still flat:
    # the ramp reads the same value half way along as it does at each end.
    xs, ps, _ = H._station_axes(solved)
    r = H._Ramp(xs, ps, smooth)
    close('%s: the solved run is still flat' % name,
          r.value(0.5 * (xs[1] + xs[2])), p, 1e-9)

# Flat ends are expressed as extra stations, so they are part of the height the
# solver aims at rather than something added on afterwards.
spec = {'stations': spring(), 'blend': 'smooth', 'slack': 2,
        'startType': 'flat', 'startPitch': 0.2, 'startFlat': 0.75, 'startBlend': 0.5,
        'endType': 'natural', 'heightMode': 'value', 'height': 8.0}
close('a flat start counts towards the pinned height',
      H.var_helix_height(H._solved_stations(spec)[0], True), 8.0, 1e-7)
ok('the flat start is not solved with the run', H._solved_group(spec), [3, 4])

print('')
print('Which stations move together')
ok('neighbours at the same pitch join the run',
   H._solved_group({'stations': spring(), 'slack': 2}), [1, 2])
ok('ticking the other one picks the same run',
   H._solved_group({'stations': spring(), 'slack': 3}), [1, 2])
ok('a station on its own stays on its own',
   H._solved_group({'stations': spring(pitch3=0.5), 'slack': 2}), [1])
ok('a closed end joins in when it really was typed the same',
   H._solved_group({'stations': spring(pitch2=0.2, pitch3=0.5), 'slack': 1}), [0, 1])
ok('nothing ticked, nothing solved',
   H._solved_group({'stations': spring(), 'slack': 0}), [])

# Whichever station is ticked, and whichever ends are on it, the table has to
# come out at the height. This is where an off-by-one in the station numbering
# shows up, because flat ends shift every station along by two.
for start_t, end_t in (('natural', 'natural'), ('flat', 'natural'),
                       ('natural', 'flat'), ('flat', 'flat')):
    for k in (1, 2, 3, 4):
        s = {'stations': spring(pitch3=0.6), 'blend': 'smooth', 'slack': k,
             'startType': start_t, 'endType': end_t, 'heightMode': 'value', 'height': 7.0,
             'startPitch': 0.2, 'startFlat': 0.75, 'startBlend': 0.5,
             'endPitch': 0.2, 'endFlat': 0.75, 'endBlend': 0.5}
        sts, pitch = H._solved_stations(s)
        close('%s/%s ends, station %d ticked' % (start_t, end_t, k),
              H.var_helix_height(sts, True), 7.0, 1e-7)

print('')
print('When it cannot be done')
# The rest of the table is already taller than the height asked for, and no
# pitch at the working coils can take height away again.
refuses('a height the rest of the table already passes',
        H.solve_group_pitch, spring(), [1, 2], 0.1, True)
refuses('nothing ticked to solve', H.solve_group_pitch, spring(), [], 6.0, True)

print('')
print('Solving twice gives the same answer')
# The solved pitch is written back into the table, so the next solve starts from
# its own answer. If that moved the answer, a helix would drift a little every
# time it was opened and shut.
pinned = {'stations': spring(), 'blend': 'smooth', 'slack': 2, 'startType': 'natural',
          'endType': 'natural', 'heightMode': 'value', 'height': 6.0}
first = H._solved_stations(pinned)[1]
baked = dict(pinned, stations=[dict(st) for st in spring()])
for i in H._solved_group(pinned):
    baked['stations'][i]['pitch'] = first
close('the second solve lands on the first', H._solved_stations(baked)[1], first, 1e-9)
ok('and on the same stations', H._solved_group(baked), H._solved_group(pinned))

# A measured line beats the typed box, so a helix follows its geometry rather
# than a number left over from before the line was picked.
close('a measured height wins over a typed one',
      H._pinned_height(dict(pinned, heightMode='axis', _axisHeight=9.0)), 9.0)
close('and the typed one is the fallback when the line has gone',
      H._pinned_height(dict(pinned, heightMode='axis')), 6.0)

print('')
print('Helices that pin nothing are left alone')
plain = {'stations': spring(), 'blend': 'smooth', 'startType': 'natural',
         'endType': 'natural'}
ok('no height mode means no solve', H._solved_stations(plain)[1], None)
ok('and the stations come back as they were',
   H._solved_stations(plain)[0], H._expanded_stations(plain))
ok('a pinned helix with no tick is left alone too',
   H._solved_stations(dict(plain, heightMode='value', height=6.0, slack=0))[1], None)
ok('so is a tick with no height',
   H._solved_stations(dict(plain, heightMode='value', slack=2))[1], None)

print('')
print('%d failed: %s' % (len(FAILED), ', '.join(FAILED)) if FAILED else 'All good.')
sys.exit(1 if FAILED else 0)
