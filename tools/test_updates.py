"""Checks for the self-update code in Helix3D.py.

Run it from anywhere, with Fusion's own Python if you have it to hand:

    python tools/test_updates.py
    python tools/test_updates.py --live

Everything runs against throwaway folders under the system temp directory. The
repository itself is only ever read, and one of the checks below is that the
add-in refuses to install over a working tree. Nothing here needs Fusion: the
add-in is imported with a stub standing in for adsk, the same trick
make_icons.py uses, and a fake Fusion supplies the message box and the custom
event queue so the whole question-and-install sequence can be driven.

Without --live nothing touches the network. With it, two calls go to GitHub to
confirm the real release still matches what the updater expects, which costs
two of the sixty unauthenticated requests an hour.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAND = os.path.join(tempfile.gettempdir(), 'helix3d-update-tests')
LIVE = '--live' in sys.argv
FAILED = []


# --------------------------------------------------------------------------
# A fake Fusion: only the handful of things the update code reaches for
# --------------------------------------------------------------------------
class _Enum:
    def __init__(self, **kw):
        self.__dict__.update(kw)


DialogResults = _Enum(DialogOK=1, DialogCancel=2, DialogYes=6, DialogNo=7)


class FakeUI:
    def __init__(self):
        self.activeCommand = 'SelectCommand'
        self.answers = []      # queued replies, taken in order
        self.boxes = []        # every box that was shown

    def messageBox(self, text, title='', buttons=0, icon=0):
        self.boxes.append((title, text))
        return self.answers.pop(0) if self.answers else DialogResults.DialogOK


class FakeApp:
    def __init__(self):
        self.userInterface = FakeUI()
        self.handlers = {}
        self.fired = []
        self.logged = []

    def log(self, msg):
        self.logged.append(msg)

    def registerCustomEvent(self, name):
        holder = self.handlers.setdefault(name, [])
        return _Enum(add=holder.append)

    def unregisterCustomEvent(self, name):
        self.handlers.pop(name, None)

    def fireCustomEvent(self, name, info=''):
        # Queued, not delivered. Fusion pumps custom events on its own thread,
        # so a test has to pump them deliberately too.
        self.fired.append((name, info))

    def pump(self):
        queued, self.fired = self.fired, []
        for name, info in queued:
            for h in self.handlers.get(name, []):
                h.notify(_Enum(additionalInfo=info))
        return len(queued)


def load_addin():
    """Import Helix3D with a stub in place of adsk."""
    for name in ('adsk', 'adsk.core', 'adsk.fusion'):
        sys.modules.setdefault(name, types.ModuleType(name))
    core, fusion = sys.modules['adsk.core'], sys.modules['adsk.fusion']
    sys.modules['adsk'].core, sys.modules['adsk'].fusion = core, fusion
    app = FakeApp()
    core.Application = _Enum(get=lambda: app)
    core.DialogResults = DialogResults
    core.MessageBoxButtonTypes = _Enum(OKButtonType=0, YesNoCancelButtonType=3)
    core.MessageBoxIconTypes = _Enum(NoIconType=0, QuestionIconType=1)
    # Anything else the add-in subclasses or casts becomes a bare object.
    core.__getattr__ = lambda name: object
    fusion.__getattr__ = lambda name: object
    sys.path.insert(0, ROOT)
    import Helix3D
    return Helix3D, app


H, APP = load_addin()
UI = APP.userInterface
GOOD_ID = H.ADDIN_ID
# Kept before anything stands in for them, so the live section can put the
# genuine ones back rather than testing the stand-ins by mistake.
REAL_FETCH, REAL_DOWNLOAD = H._fetch_latest, H._download


def ok(label, got, want=True):
    if got != want:
        FAILED.append(label)
        print('  FAIL %s: got %r, wanted %r' % (label, got, want))
    else:
        print('  ok   %s' % label)


def refuses(label, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except H._UpdateError as e:
        print('  ok   %s (%s)' % (label, e.reason))
        return
    except Exception as e:
        FAILED.append(label)
        print('  FAIL %s: raised %s: %s' % (label, type(e).__name__, e))
        return
    FAILED.append(label)
    print('  FAIL %s: was accepted' % label)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def read(path):
    with io.open(path, encoding='utf-8') as f:
        return f.read()


def use(folder):
    """Point the add-in at a throwaway folder instead of its own."""
    os.makedirs(folder, exist_ok=True)
    H._addin_dir = lambda: folder
    H.SETTINGS_FILE = os.path.join(folder, 'Helix3D.settings.json')
    H.LOG_FILE = os.path.join(folder, 'Helix3D.log')
    return folder


shutil.rmtree(SAND, ignore_errors=True)
os.makedirs(SAND)
use(os.path.join(SAND, 'scratch'))


# --------------------------------------------------------------------------
print('version numbers')
# --------------------------------------------------------------------------
ok('plain', H._parse_version('0.8.0'), (0, 8, 0, 0, 1))
ok('v prefix', H._parse_version('v0.8.0'), (0, 8, 0, 0, 1))
ok('two parts', H._parse_version('0.8'), (0, 8, 0, 0, 1))
ok('four parts', H._parse_version('0.8.0.1'), (0, 8, 0, 1, 1))
ok('junk', H._parse_version('nonsense'), None)
ok('empty', H._parse_version(''), None)
ok('not a string', H._parse_version(None), None)
ok('newer', H._newer('0.9.0', '0.8.0'))
ok('same is not newer', H._newer('0.8.0', '0.8.0'), False)
ok('older is not newer', H._newer('0.7.7', '0.8.0'), False)
ok('ten beats nine', H._newer('0.10.0', '0.9.0'))
ok('a date style version works', H._newer('2026.1', '0.8.0'))
ok('junk never triggers an update', H._newer('bad', '0.8.0'), False)
ok('nor on the other side', H._newer('0.9.0', 'bad'), False)
ok('a release beats its own rc', H._newer('0.9.0', '0.9.0-rc1'))
ok('and the rc does not beat it', H._newer('0.9.0-rc1', '0.9.0'), False)


# --------------------------------------------------------------------------
print('which kind of install')
# --------------------------------------------------------------------------
ok('this repository is a working tree', H._is_dev_install(ROOT))
ok('a throwaway folder is not', H._is_dev_install(os.path.join(SAND, 'scratch')), False)
for marker in ('.git', '.github', '.gitignore', 'tools'):
    p = os.path.join(SAND, 'scratch', marker)
    io.open(p, 'w').close()
    ok('spotted by ' + marker, H._is_dev_install(os.path.join(SAND, 'scratch')))
    os.remove(p)
ok('an app store bundle', H._is_managed_install(
    r'C:\ProgramData\Autodesk\ApplicationPlugins\Thing.bundle'))
ok('the ordinary add-ins folder', H._is_managed_install(
    r'C:\Users\x\AppData\Roaming\Autodesk\Autodesk Fusion 360\API\AddIns\Helix3D'), False)
ok('the repository refuses to be updated', H._may_install(ROOT)[0], False)


# --------------------------------------------------------------------------
print('how often it looks')
# --------------------------------------------------------------------------
H._settings.clear()
u = H._upd()
ok('the settings gain every key', sorted(u), sorted(H.UPDATE_DEFAULTS))
ok('due when it has never looked', H._update_due(1000.0))
u['nextCheck'] = 2000.0
ok('not due yet', H._update_due(1000.0), False)
ok('due once the time passes', H._update_due(2000.0))
u['nextCheck'] = 1000.0 + 400 * 86400
ok('a clock that jumped backwards is ignored', H._update_due(1000.0))
u['nextCheck'], u['enabled'] = 0.0, False
ok('switched off in the settings', H._update_due(1e9), False)
u['enabled'], u['failures'] = True, 0
for n, days in ((1, 1), (2, 3), (3, 7), (4, 7)):
    H._record_failure('offline')
    ok('%d failure(s) backs off %d days' % (n, days),
       round(u['nextCheck'] - time.time()), days * 86400)
H._record_failure('rate limited', until=time.time() + 30 * 86400)
ok('a rate limit reset overrides the backoff',
   round(u['nextCheck'] - time.time()), 30 * 86400)
H._record_success('W/"abc"')
ok('success clears the failures', u['failures'], 0)
ok('and stores the etag', u['etag'], 'W/"abc"')
ok('and comes back in a day', round(u['nextCheck'] - time.time()), 86400)


# --------------------------------------------------------------------------
print('addresses it will fetch')
# --------------------------------------------------------------------------
ok('the api', H._host_ok(H.GH_LATEST))
ok('the site', H._host_ok('https://github.com/a/b'))
ok('where assets actually redirect to', H._host_ok(
    'https://release-assets.githubusercontent.com/github-production-release-asset/1/2'))
ok('plain http is refused', H._host_ok('http://github.com/a'), False)
ok('a username in front of the host', H._host_ok('https://github.com@evil.example/x'), False)
ok('with a password too', H._host_ok('https://github.com:pw@evil.example/x'), False)
ok('a lookalike host', H._host_ok('https://notgithub.com/x'), False)
ok('another lookalike', H._host_ok('https://evilgithubusercontent.com/x'), False)
ok('the name as a subdomain', H._host_ok('https://github.com.evil.example/x'), False)
ok('a local file', H._host_ok('file:///c:/windows/system32'), False)
ok('nonsense', H._host_ok('not a url'), False)
ok('uppercase is still fine', H._host_ok('https://API.GITHUB.COM/x'))


# --------------------------------------------------------------------------
print('certificates, including the Mac fallbacks')
# --------------------------------------------------------------------------
# The Mac path cannot be run on a Mac from here, so what is checked is the
# ladder itself: that each rung is tried, that a rung which finds nothing
# falls through, that the bottom of the ladder is None, and above all that no
# rung ever hands back a context which has stopped verifying.
import ssl as _ssl                                                   # noqa: E402

_real_default = _ssl.create_default_context
_real_platform = sys.platform
_real_keychain = H._macos_keychain_roots


def _empty_context(*a, **kw):
    """What CPython hands back where it cannot find a trust store: a context
    that still wants to verify, with nothing to verify against."""
    return _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)


def _some_real_pem():
    """Real certificates in PEM form, borrowed from whatever this machine
    trusts, so the keychain rung has something genuine to load."""
    out = []
    for der in _real_default().get_ca_certs(binary_form=True)[:20]:
        out.append(_ssl.DER_cert_to_PEM_cert(der))
    return ''.join(out)


def verifying(ctx):
    return (ctx is not None and bool(ctx.get_ca_certs())
            and ctx.check_hostname and ctx.verify_mode == _ssl.CERT_REQUIRED)


H._ssl_ctx = False
ok('on this machine, as it stands', verifying(H._ssl_context()))
ok('and it is cached', H._ssl_context() is H._ssl_context())

# Gathered before sys.platform is faked, and this is not fussiness. CPython
# only reads the operating system's certificate store when it believes it is
# on Windows, so pretending to be a Mac empties the very store this borrows
# from. That is the exact behaviour the Mac ladder below exists to work round.
_PEM = _some_real_pem()
ok('real certificates to test the ladder with', _PEM.count('BEGIN CERT') > 0)

try:
    _ssl.create_default_context = _empty_context

    H._ssl_ctx = False
    sys.platform = 'linux'
    ok('no trust store and nowhere to look gives up', H._ssl_context(), None)

    H._ssl_ctx = False
    sys.platform = 'darwin'
    H._macos_keychain_roots = lambda: ''
    ok('on a Mac with nothing in the keychain either', H._ssl_context(), None)

    H._ssl_ctx = False
    H._macos_keychain_roots = lambda: _PEM
    got = H._ssl_context()
    ok('but the keychain rung loads what it finds', verifying(got))
    ok('and it still checks the hostname', got is not None and got.check_hostname)
    ok('and still demands a valid chain',
       got is not None and got.verify_mode == _ssl.CERT_REQUIRED)

    H._ssl_ctx = False
    H._macos_keychain_roots = lambda: 'not a certificate at all'
    ok('rubbish from the keychain is refused, not trusted', H._ssl_context(), None)

    H._ssl_ctx = False
    H._macos_keychain_roots = lambda: (_ for _ in ()).throw(OSError('security missing'))
    try:
        ok('and a keychain that errors gives up quietly', H._ssl_context(), None)
    except OSError:
        ok('and a keychain that errors gives up quietly', 'it raised instead', None)
finally:
    _ssl.create_default_context = _real_default
    sys.platform = _real_platform
    H._macos_keychain_roots = _real_keychain
    H._ssl_ctx = False

ok('no path anywhere builds an unverified context',
   'ssl._create_unverified_context' not in read(os.path.join(ROOT, 'Helix3D.py')))
ok('nor turns hostname checking off',
   'check_hostname = False' not in read(os.path.join(ROOT, 'Helix3D.py')))
ok('nor accepts any certificate',
   'CERT_NONE' not in read(os.path.join(ROOT, 'Helix3D.py')))


# --------------------------------------------------------------------------
print('paths inside a zip')
# --------------------------------------------------------------------------
top = os.path.realpath(os.path.join(SAND, 'scratch'))
ok('an ordinary file', H._safe_member(top, 'Helix3D/Helix3D.py') is not None)
ok('one in a subfolder', H._safe_member(top, 'Helix3D/resources/a.png') is not None)
ok('climbing out', H._safe_member(top, 'Helix3D/../../evil.py'), None)
ok('starting outside', H._safe_member(top, '../evil.py'), None)
ok('an absolute path', H._safe_member(top, '/etc/passwd'), None)
ok('a drive letter', H._safe_member(top, 'C:/windows/evil.py'), None)
ok('backslashes, which only escape on Windows',
   H._safe_member(top, 'Helix3D\\..\\..\\evil.py'), None)
ok('a bare backslash', H._safe_member(top, 'a\\b.py'), None)
ok('a folder entry', H._safe_member(top, 'Helix3D/'), None)
ok('nothing at all', H._safe_member(top, ''), None)


# --------------------------------------------------------------------------
print('unpacking a release')
# --------------------------------------------------------------------------
def release_zip(version='0.9.0', top_name='Helix3D', addin_id=None, kind='addin',
                source=None, extra=(), pad=12000):
    buf = io.BytesIO()
    # The version is stamped into the code as well as the manifest, so a check
    # can tell the new file apart from the one it replaced.
    source = '# %s\nx = 1\n' % version if source is None else source
    with zipfile.ZipFile(buf, 'w') as z:
        if top_name is not None:
            z.writestr('%s/Helix3D.py' % top_name, source)
            z.writestr('%s/Helix3D.manifest' % top_name, json.dumps(
                {'type': kind, 'id': addin_id or GOOD_ID, 'version': version}))
            z.writestr('%s/resources/16x16.png' % top_name, 'icon %s\n' % version)
            # Padded past the size floor. A real release is about 30 KB and a
            # few hundred bytes would rightly be refused.
            z.writestr('%s/README.md' % top_name, 'readme\n' + 'x' * pad)
        for name, data in extra:
            z.writestr(name, data)
    return buf.getvalue()


STAGE = os.path.join(SAND, 'stage')
staged = H._stage_release(release_zip(), 'v0.9.0', STAGE)
ok('a good one unpacks', os.path.isfile(os.path.join(staged, 'Helix3D.py')))
ok('with its resources', os.path.isfile(os.path.join(staged, 'resources', '16x16.png')))
ok('and its manifest', H._manifest_version(os.path.join(staged, 'Helix3D.manifest')), '0.9.0')

refuses('the tag and the manifest disagree', H._stage_release,
        release_zip('0.9.0'), 'v0.9.1', STAGE)
refuses('a different add-in entirely', H._stage_release,
        release_zip(addin_id='11111111-2222-3333-4444-555555555555'), 'v0.9.0', STAGE)
refuses('not an add-in', H._stage_release, release_zip(kind='script'), 'v0.9.0', STAGE)
refuses('the wrong top folder', H._stage_release,
        release_zip(top_name='Helix3DX'), 'v0.9.0', STAGE)
refuses('two top folders', H._stage_release,
        release_zip(extra=[('Other/a.txt', 'x')]), 'v0.9.0', STAGE)
refuses('a loose file at the top', H._stage_release,
        release_zip(extra=[('readme.txt', 'x')]), 'v0.9.0', STAGE)
refuses('code that did not arrive whole', H._stage_release,
        release_zip(source='def f(:\n'), 'v0.9.0', STAGE)
refuses('an entry climbing out of the folder', H._stage_release,
        release_zip(extra=[('Helix3D/../../evil.py', 'boom')]), 'v0.9.0', STAGE)
refuses('far too many files', H._stage_release,
        release_zip(extra=[('Helix3D/f%d.txt' % i, 'x') for i in range(250)]), 'v0.9.0', STAGE)
refuses('not a zip at all', H._stage_release, b'this is not a zip', 'v0.9.0', STAGE)

buf = io.BytesIO()
with zipfile.ZipFile(buf, 'w') as z:
    z.writestr('Helix3D/Helix3D.py', 'x = 1\n')
    z.writestr('Helix3D/Helix3D.manifest', json.dumps(
        {'type': 'addin', 'id': GOOD_ID, 'version': '0.9.0'}))
    info = zipfile.ZipInfo('Helix3D/link.py')
    info.external_attr = 0o120777 << 16
    z.writestr(info, '../../../evil.py')
refuses('a link where a file should be', H._stage_release, buf.getvalue(), 'v0.9.0', STAGE)

buf = io.BytesIO()
with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
    z.writestr('Helix3D/Helix3D.py', 'x = 1\n')
    z.writestr('Helix3D/Helix3D.manifest', json.dumps(
        {'type': 'addin', 'id': GOOD_ID, 'version': '0.9.0'}))
    z.writestr('Helix3D/bomb.bin', b'\0' * 60_000_000)
refuses('a zip bomb', H._stage_release, buf.getvalue(), 'v0.9.0', STAGE)


# --------------------------------------------------------------------------
print('replacing the files')
# --------------------------------------------------------------------------
def make_install(name, version='0.7.6'):
    """An add-in folder as a user would have it, with the two files that have
    to come through an update untouched."""
    folder = os.path.join(SAND, name)
    shutil.rmtree(folder, ignore_errors=True)
    write(os.path.join(folder, 'Helix3D.py'), '# version %s\nx = 1\n' % version)
    write(os.path.join(folder, 'Helix3D.manifest'), json.dumps(
        {'type': 'addin', 'id': GOOD_ID, 'version': version}))
    write(os.path.join(folder, 'README.md'), 'old readme %s\n' % version)
    write(os.path.join(folder, 'LICENSE'), 'MIT\n')
    write(os.path.join(folder, 'resources', '16x16.png'), 'old icon\n')
    write(os.path.join(folder, 'Helix3D.settings.json'), json.dumps({'placementExpanded': True}))
    write(os.path.join(folder, 'Helix3D.log'), 'earlier log lines\n')
    write(os.path.join(folder, '__pycache__', 'Helix3D.cpython-314.pyc'), 'stale\n')
    return folder


def make_staged(name, version='0.9.0'):
    folder = os.path.join(SAND, name)
    shutil.rmtree(folder, ignore_errors=True)
    write(os.path.join(folder, 'Helix3D.py'), '# version %s\nx = 2\n' % version)
    write(os.path.join(folder, 'Helix3D.manifest'), json.dumps(
        {'type': 'addin', 'id': GOOD_ID, 'version': version}))
    write(os.path.join(folder, 'README.md'), 'new readme %s\n' % version)
    write(os.path.join(folder, 'LICENSE'), 'MIT\n')
    write(os.path.join(folder, 'resources', '16x16.png'), 'new icon\n')
    write(os.path.join(folder, 'resources', 'toolclips', 'pitch.png'), 'brand new\n')
    return folder


new = make_staged('staged')
plan = [rel for _, rel in H._plan_files(new)]
ok('everything is planned', sorted(plan), sorted(
    ['Helix3D.py', 'Helix3D.manifest', 'README.md', 'LICENSE',
     'resources/16x16.png', 'resources/toolclips/pitch.png']))
ok('the code goes second to last', plan[-2], 'Helix3D.py')
ok('the manifest goes last of all', plan[-1], 'Helix3D.manifest')

live = use(make_install('live'))
ok('every file copied', H._install(new, live), 6)
ok('new code in place', read(os.path.join(live, 'Helix3D.py')), '# version 0.9.0\nx = 2\n')
ok('new manifest in place', H._manifest_version(), '0.9.0')
ok('new readme in place', read(os.path.join(live, 'README.md')), 'new readme 0.9.0\n')
ok('icon replaced', read(os.path.join(live, 'resources', '16x16.png')), 'new icon\n')
ok('a folder that did not exist before', read(os.path.join(
    live, 'resources', 'toolclips', 'pitch.png')), 'brand new\n')
ok('the settings file came through', json.load(
    io.open(os.path.join(live, 'Helix3D.settings.json')))['placementExpanded'], True)
ok('and so did the log', read(os.path.join(live, 'Helix3D.log')), 'earlier log lines\n')
ok('the stale bytecode is gone', os.path.isdir(os.path.join(live, '__pycache__')), False)
ok('a way back was kept', read(os.path.join(
    live, '.helix3d-backup', 'Helix3D.py')), '# version 0.7.6\nx = 1\n')
ok('no half-written files left', [f for f in os.listdir(live) if 'tmp' in f], [])
ok('the lock was released', os.path.isfile(os.path.join(live, '.helix3d-update', 'lock')), False)

live2 = use(make_install('live2'))
real_replace = H._replace_file


def fail_on_code(src, dst, attempts=5):
    if os.path.basename(dst) == 'Helix3D.py':
        raise H._UpdateError('pretend something has it open')
    return real_replace(src, dst, attempts)


H._replace_file = fail_on_code
refuses('a copy that fails partway', H._install, make_staged('staged2'), live2)
H._replace_file = real_replace
ok('the old code is back', read(os.path.join(live2, 'Helix3D.py')), '# version 0.7.6\nx = 1\n')
ok('the old manifest is back', H._manifest_version(), '0.7.6')
ok('the old readme is back', read(os.path.join(live2, 'README.md')), 'old readme 0.7.6\n')
ok('the old icon is back', read(os.path.join(live2, 'resources', '16x16.png')), 'old icon\n')
ok('the settings survived that too', json.load(
    io.open(os.path.join(live2, 'Helix3D.settings.json')))['placementExpanded'], True)
ok('nothing was recorded as broken', H._upd()['broken'], '')

live3 = use(make_install('live3'))
real_open = H.open if hasattr(H, 'open') else open


def deny_write(path, mode='r', *a, **kw):
    if mode == 'r+b':
        raise PermissionError(13, 'Access is denied')
    return real_open(path, mode, *a, **kw)


H.open = deny_write
refuses('a folder it cannot write to', H._install, new, live3)
del H.open
ok('nothing was touched', read(os.path.join(live3, 'Helix3D.py')), '# version 0.7.6\nx = 1\n')
ok('and no backup was taken', os.path.exists(os.path.join(live3, '.helix3d-backup')), False)

live4 = use(make_install('live4'))
held = H._take_lock(os.path.join(live4, '.helix3d-update'))
refuses('a second Fusion window installing at once', H._install, new, live4)
ok('it left the files alone', read(os.path.join(live4, 'Helix3D.py')), '# version 0.7.6\nx = 1\n')
H._release_lock(held)
ok('and goes ahead once the first is done', H._install(new, live4), 6)

live5 = use(make_install('live5'))
stale = H._take_lock(os.path.join(live5, '.helix3d-update'))
os.utime(stale, (0, 0))
ok('a lock left by a process that died', H._install(new, live5), 6)

ok('the repository was never written to', os.path.exists(
    os.path.join(ROOT, '.helix3d-update')), False)
ok('nor given a backup folder', os.path.exists(os.path.join(ROOT, '.helix3d-backup')), False)


# --------------------------------------------------------------------------
print('the whole sequence, start to finish')
# --------------------------------------------------------------------------
FLOW = use(os.path.join(SAND, 'flow'))


def seed(version='0.8.0', allow_dev=True):
    """A pretend install of `version`.

    allow_dev writes the hand-edited override into the settings file. It is
    needed for every sequence that installs, because Helix3D.py itself is
    sitting in this repository and the guard rightly refuses on that alone,
    whatever folder the rest of the code is pointed at. The one sequence that
    checks the guard still bites passes allow_dev=False."""
    shutil.rmtree(FLOW, ignore_errors=True)
    use(FLOW)
    write(os.path.join(FLOW, 'Helix3D.py'), '# %s\n' % version)
    write(os.path.join(FLOW, 'Helix3D.manifest'), json.dumps(
        {'type': 'addin', 'id': GOOD_ID, 'version': version}))
    write(os.path.join(FLOW, 'resources', '16x16.png'), 'icon %s\n' % version)
    write(os.path.join(FLOW, 'Helix3D.settings.json'), json.dumps(
        {'placementExpanded': True, 'update': {'allowDevInstall': allow_dev}}))


def fake_github(version='0.9.0', fail=None):
    """Stand in for the two network calls, so the sequence can be driven
    without spending the rate limit or depending on what is published."""
    blob = release_zip(version)
    url = ('https://github.com/RonnyM82/AutodeskFusionHelix3D/releases/'
           'download/v%s/Helix3D-%s.zip' % (version, version))

    def fetch(etag=''):
        if fail == 'fetch':
            raise H._UpdateError('Could not reach GitHub.')
        return ({'tag_name': 'v%s' % version,
                 'assets': [{'name': 'Helix3D-%s.zip' % version, 'size': len(blob),
                             'browser_download_url': url}]}, 'W/"etag-%s"' % version)

    def download(url_, size):
        if fail == 'download':
            raise H._UpdateError('The download was interrupted.')
        return blob

    H._fetch_latest, H._download = fetch, download


def session():
    """Start Fusion again on the folder as it now stands."""
    H._settings.clear()
    try:
        H._settings.update(json.load(io.open(os.path.join(FLOW, 'Helix3D.settings.json'))))
    except (OSError, ValueError):
        pass
    H._VERSION = H._manifest_version()
    H._update_ready, H._update_asked = None, False
    H._pending_confirmed[0] = ''
    UI.boxes, UI.answers, UI.activeCommand = [], [], 'SelectCommand'
    APP.unregisterCustomEvent(H.UPDATE_EVT)           # exactly as run() does
    APP.registerCustomEvent(H.UPDATE_EVT).add(H.UpdateEvent())
    # _update_startup would launch a check of its own, which the checks below
    # would then be racing. Hold it off, then restore the real due time.
    due = H._upd().get('nextCheck', 0)
    H._upd()['nextCheck'] = time.time() + 1e6
    H._update_startup()
    H._upd()['nextCheck'] = due
    APP.fired = []


def check(force=True):
    """One full cycle: start the worker, wait for it, deliver the event."""
    H._start_update_check(force=force)
    for _ in range(300):
        if APP.fired:
            break
        time.sleep(0.01)
    for t in threading.enumerate():
        if t is not threading.current_thread() and t.daemon:
            t.join(timeout=3)
    return APP.pump()


def finish_a_helix_command():
    """What CommandTerminated does when one of our commands ends."""
    H._request_drain()
    APP.pump()


seed('0.8.0')
fake_github('0.8.0')
session()
check()
ok('nothing new, nothing to offer', H._update_ready, None)
finish_a_helix_command()
ok('and nothing is said', UI.boxes, [])

fake_github('0.9.0')
session()
check()
ok('a new one is downloaded quietly', H._update_ready[0], '0.9.0')
ok('still nothing on screen', UI.boxes, [])
ok('and nothing is recorded as installed', H._upd()['pendingVersion'], '')
UI.activeCommand = 'scottHelix3DCreate'
finish_a_helix_command()
ok('it waits while a dialog is open', UI.boxes, [])
UI.activeCommand = 'SelectCommand'
UI.answers = [DialogResults.DialogNo]
finish_a_helix_command()
ok('then asks, once', len(UI.boxes), 1)
ok('naming both versions', '0.9.0' in UI.boxes[0][1] and '0.8.0' in UI.boxes[0][1])
ok('Later installs nothing', H._manifest_version(), '0.8.0')
ok('and clears the download', os.path.exists(H._stage_dir()), False)
UI.boxes = []
finish_a_helix_command()
ok('and it does not ask twice in a session', UI.boxes, [])
# The one that bit in real use: a version left unanswered must not start the
# day-long wait, or restarting Fusion finds nothing to do and says nothing.
ok('Later leaves the next check due straight away', H._upd()['nextCheck'], 0.0)
session()
ok('so the next session checks again', H._update_due())
check(force=False)
ok('and offers the same version again', H._update_ready[0], '0.9.0')
UI.answers = [DialogResults.DialogNo]
finish_a_helix_command()
ok('asking a second time', len(UI.boxes), 1)
ok('and it says which next time it means', 'next time Fusion starts' in UI.boxes[0][1])

# Never opening a helix command has to behave the same way: the question was
# never put, so the offer cannot be treated as dealt with.
fake_github('0.9.0')
session()
check()
ok('a version nobody was asked about holds the check open', H._upd()['nextCheck'], 0.0)
session()
fake_github('0.8.0')
check(force=False)
ok('but once there is nothing to say, the day starts',
   round(H._upd()['nextCheck'] - time.time()), 86400)

fake_github('0.9.0')      # the check above left the stand-in on 0.8.0
session()
check()
UI.answers = [DialogResults.DialogCancel]
finish_a_helix_command()
ok('Skip is remembered', H._upd()['skipVersion'], '0.9.0')
session()
check()
ok('so that version is not offered again', H._update_ready, None)
fake_github('0.9.1')
session()
check()
ok('but a later one still is', H._update_ready[0], '0.9.1')
H._upd()['skipVersion'] = ''
H._save_settings()

fake_github('0.9.0')
session()
check()
UI.answers = [DialogResults.DialogYes]
finish_a_helix_command()
ok('the question, then the result', len(UI.boxes), 2)
ok('it says to restart Fusion', 'Restart Fusion' in UI.boxes[1][1])
ok('and never says restart the add-in', 'add-in' in UI.boxes[1][1], False)
ok('the code on disk moved', read(os.path.join(FLOW, 'Helix3D.py')).startswith('# 0.9.0'))
ok('the manifest on disk moved', H._manifest_version(), '0.9.0')
ok('the settings file came through', json.load(
    io.open(os.path.join(FLOW, 'Helix3D.settings.json')))['placementExpanded'], True)
ok('recorded as waiting for a restart', H._upd()['pendingVersion'], '0.9.0')
ok('a way back was kept', os.path.isfile(os.path.join(FLOW, '.helix3d-backup', 'Helix3D.py')))
ok('the session still knows it is the old one', H._restart_pending(), '0.9.0')
check()
ok('and will not offer it twice', H._update_ready, None)

session()
ok('after a restart it is the new one', H._VERSION, '0.9.0')
ok('nothing pending any more', H._restart_pending(), '')
ok('the flag was cleared', H._upd()['pendingVersion'], '')
finish_a_helix_command()
ok('it confirms the upgrade', len(UI.boxes), 1)
ok('naming the version', '0.9.0' in UI.boxes[0][1])
UI.boxes = []
finish_a_helix_command()
ok('and only the once', UI.boxes, [])

seed('0.8.0', allow_dev=False)
fake_github('0.9.0')
session()
H._addin_dir = lambda: ROOT
check()
ok('running from the repository, nothing is downloaded', H._update_ready, None)
finish_a_helix_command()
ok('and no dialog appears', UI.boxes, [])
ok('it goes in the log instead', any('source repository' in m for m in APP.logged))
ok('and the repository is left alone', os.path.exists(os.path.join(ROOT, '.helix3d-update')), False)
use(FLOW)

seed('0.8.0')
fake_github('0.9.0', fail='fetch')
session()
check()
finish_a_helix_command()
ok('an unreachable GitHub says nothing', UI.boxes, [])
ok('but is counted', H._upd()['failures'], 1)
ok('and backs off a day', round(H._upd()['nextCheck'] - time.time()), 86400)

seed('0.8.0')
fake_github('0.9.0', fail='download')
session()
check()
finish_a_helix_command()
ok('a broken download says nothing either', UI.boxes, [])
ok('and is counted too', H._upd()['failures'] >= 1)

seed('0.8.0')
fake_github('0.9.0')
session()
H._upd()['failures'] = 0
H._upd()['nextCheck'] = time.time() + 3600
check(force=False)
ok('a check that is not due does nothing', H._update_ready, None)
H._upd()['nextCheck'] = 0
check(force=False)
ok('and runs once it is due', H._update_ready[0], '0.9.0')

H._update_ready = None
H._stop_update_check()
APP.fired.append((H.UPDATE_EVT, json.dumps(
    {'gen': -1, 'state': 'ready', 'version': '9.9.9', 'staged': 'nowhere'})))
APP.pump()
ok('an answer from a previous load is ignored', H._update_ready, None)


# --------------------------------------------------------------------------
if LIVE:
    print('against the real GitHub')
    H._fetch_latest, H._download = REAL_FETCH, REAL_DOWNLOAD
    ctx = H._ssl_context()
    ok('a verifying connection is possible', ctx is not None)
    if ctx is not None:
        import ssl
        print('       %d trust anchors, %s, %s'
              % (len(ctx.get_ca_certs()), sys.platform, ssl.OPENSSL_VERSION))
        release, etag = H._fetch_latest()
        ok('a release came back', isinstance(release, dict))
        tag = release['tag_name']
        print('       latest published release is %s' % tag)
        ok('its tag reads as a version', H._parse_version(tag) is not None)
        url, size = H._pick_asset(release, tag)
        ok('its download is on GitHub', H._host_ok(url))
        ok('and is a plausible size', H.MIN_ZIP <= size <= H.MAX_ZIP)
        refuses('a tag with no matching asset', H._pick_asset, release, 'v9.9.9')
        blob = H._download(url, size)
        ok('it downloads at the stated size', len(blob), size)
        refuses('a size that does not match', H._download, url, size + 1)
        got = H._stage_release(blob, tag, STAGE)
        ok('the real release unpacks and passes every check',
           os.path.isfile(os.path.join(got, 'Helix3D.py')))
        ok('and its manifest matches the tag',
           H._manifest_version(os.path.join(got, 'Helix3D.manifest')), tag.lstrip('v'))
else:
    print('against the real GitHub')
    print('  skip  (pass --live to spend two of the sixty requests an hour)')

shutil.rmtree(SAND, ignore_errors=True)
print()
if FAILED:
    print('%d FAILED: %s' % (len(FAILED), ', '.join(FAILED)))
    sys.exit(1)
print('all passed')
