"""Camera driver check + installation (no Qt imports).

Two ways to get the camera onto a driver libusb can open:
  1. the driver that ships with Windows (winusb.sys / winusb.inf): bound with SetupAPI in winusb.py, one UAC prompt,
     nothing downloaded (install_inbox_winusb / remove_inbox_winusb below);
  2. the vendor's libusbK package, described next (install_from_download).

The P2 Pro (USB 0BDA:5840) needs the libusbK driver from the vendor's `P2Pro.inf`. The vendor only ships it
inside the Windows PC app installer, which we fetch from the Thermal Master download centre:

    download-center page  ->  link "Temp Master (P-Serie)"  ->  Inno Setup 6 .exe  ->  {app}\\drivers\\inf\\P2Pro.inf

The installer is never executed: its contents are unpacked with the pure-Python reader in inno.py, only the
`drivers` folder is extracted, and the driver is installed with `pnputil` elevated through a UAC prompt.
"""
import contextlib
import html as htmllib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import inno
import winusb

VID_PID = 'VID_0BDA&PID_5840'
INF_NAME = 'P2Pro.inf'
PAGE_URL = 'https://thermalmaster.com/de-de/pages/download-center'
ANCHOR_TEXT = 'Temp Master (P-Serie)'
# Last known good link (2026-11-01), used when the page can't be fetched or parsed.
FALLBACK_URL = 'https://infrared-thermal-camera.com/app/pc-software/Thermal%20Master-Setup_1.3.12.3.exe'
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
              'Chrome/124.0 Safari/537.36')
MAX_DOWNLOAD = 400 * 1024 * 1024        # the installer is ~54 MB; refuse anything absurd
MAX_UNPACKED = 1024 * 1024 * 1024
GOOD_SERVICES = ('libusbk', 'winusb')   # what libusb-1.0 (pyusb backend) can open on Windows
DRIVER_DIR_NAMES = ('drivers', 'driver', 'treiber')
CREATE_NO_WINDOW = 0x08000000

# pnputil exit codes we treat as success
PNPUTIL_OK = 0
PNPUTIL_REBOOT = 3010           # ERROR_SUCCESS_REBOOT_REQUIRED
PNPUTIL_NO_MORE_ITEMS = 259     # ERROR_NO_MORE_ITEMS: package added, no matching device present / already current
ERROR_CANCELLED = 1223


class DriverError(Exception):
    """Failure with a message that can be shown to the user as is."""


class DriverCancelled(DriverError):
    pass


# --------------------------------------------------------------------------------- detection
def is_windows():
    return sys.platform == 'win32'


def _oem_encoding():
    try:
        import ctypes
        return 'cp%d' % ctypes.windll.kernel32.GetOEMCP()
    except Exception:
        return 'cp850'


def _run(args, timeout=20):
    """Run a console tool without a window; returns (returncode, text) with OEM-codepage decoding."""
    kw = {'creationflags': CREATE_NO_WINDOW} if is_windows() else {}
    p = subprocess.run(args, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, **kw)
    return p.returncode, (p.stdout + p.stderr).decode(_oem_encoding(), errors='replace')


def driver_store_has(pnputil_output, name=INF_NAME):
    """True if `pnputil /enum-drivers` output lists a package whose original name is `name`.

    Labels are localized ("Original Name:" / "Originalname:" / ...), so we don't look at the label: any
    `<label>: <something>.inf` line whose value equals `name` counts. (The "Published Name: oemNN.inf" line
    never equals the vendor's file name.)"""
    want = name.lower()
    for line in pnputil_output.splitlines():
        if ':' not in line:
            continue
        value = line.split(':', 1)[1].strip().lower()
        if value == want:
            return True
    return False


def service_is_usable(service):
    return bool(service) and service.strip().lower() in GOOD_SERVICES


def _store_check():
    """True/False from the driver store, None if pnputil is unavailable or failed."""
    for args in (['pnputil', '/enum-drivers'], ['pnputil.exe', '-e']):    # new syntax, then Windows 7/8 syntax
        try:
            rc, out = _run(args)
        except (OSError, subprocess.SubprocessError):
            continue
        if rc == 0 and out.strip():
            return driver_store_has(out)
    return None


def _registry_check():
    """True if the camera has been enumerated with a libusbK/WinUSB service, False if only with something else,
    None if it never showed up (or the registry can't be read)."""
    try:
        import winreg
        base = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Enum\USB\\' + VID_PID)
    except OSError:
        return None
    seen = False
    with base:
        i = 0
        while True:
            try:
                inst = winreg.EnumKey(base, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(base, inst) as k:
                    svc = winreg.QueryValueEx(k, 'Service')[0]
            except OSError:
                svc = ''
            seen = True
            if service_is_usable(svc):
                return True
    return False if seen else None


def is_driver_installed():
    """True: nothing to do. False: driver missing. None: can't tell."""
    if not is_windows():
        return True
    try:
        if winusb.binding_state() in ('winusb', 'libusbk'):      # the connected camera is bound to a usable driver
            return True
        store = _store_check()
        if store:
            return True
        reg = _registry_check()
        if reg:
            return True
        if store is False:
            return False
        return False if reg is False else None
    except Exception:
        return None


# --------------------------------------------------------------------------------- download page
_A_RE = re.compile(r'<a\b([^>]*)>(.*?)</a\s*>', re.I | re.S)
_HREF_RE = re.compile(r'''\bhref\s*=\s*(["'])(.*?)\1''', re.I | re.S)
_TAG_RE = re.compile(r'<[^>]*>')
_ANCHOR_RE = re.compile(r'temp\s*master\s*\(\s*p[\s-]*seri(?:e|es)\s*\)', re.I)


def find_download_url(page_html, base=PAGE_URL):
    """Return the absolute URL of the link whose text is "Temp Master (P-Serie)"."""
    for m in _A_RE.finditer(page_html):
        text = htmllib.unescape(_TAG_RE.sub(' ', re.sub(r'<\?.*?\?>', ' ', m.group(2), flags=re.S)))
        if not _ANCHOR_RE.search(' '.join(text.split())):
            continue
        h = _HREF_RE.search(m.group(1))
        if not h:
            continue
        url = urllib.parse.urljoin(base, htmllib.unescape(h.group(2)).strip())
        if urllib.parse.urlsplit(url).scheme != 'https':
            raise DriverError(f'download link is not https: {url}')
        return url
    raise DriverError(f'link "{ANCHOR_TEXT}" not found on the download page')


def _open(url, timeout=30):
    url = urllib.parse.quote(url, safe=":/?&=%()+~,;@!$'*#[]")
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': '*/*'})
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_download_url(log=None):
    """Find the installer link on the live download page; falls back to FALLBACK_URL."""
    try:
        with _open(PAGE_URL, 20) as r:
            page = r.read(8 * 1024 * 1024).decode('utf-8', errors='replace')
        return find_download_url(page)
    except (DriverError, OSError, ValueError) as e:      # URLError/timeouts are OSErrors
        if log:
            log(f'download page unusable ({e}); using fallback link')
        return FALLBACK_URL


def download(url, dest, progress=None, cancel=None):
    """Download url to dest (via dest+'.part'). progress(done, total_or_0); cancel: threading.Event or callable.
    Raises DriverCancelled / DriverError."""
    def cancelled():
        return cancel.is_set() if hasattr(cancel, 'is_set') else bool(cancel and cancel())
    part = dest + '.part'
    try:
        with _open(url, 30) as r, open(part, 'wb') as out:
            total = int(r.headers.get('Content-Length') or 0)
            if total > MAX_DOWNLOAD:
                raise DriverError('download is unexpectedly large, aborting')
            done = 0
            while True:
                if cancelled():
                    raise DriverCancelled('cancelled')
                buf = r.read(64 * 1024)
                if not buf:
                    break
                out.write(buf)
                done += len(buf)
                if done > MAX_DOWNLOAD:
                    raise DriverError('download is unexpectedly large, aborting')
                if progress:
                    progress(done, total)
        if total and done != total:
            raise DriverError('download incomplete')
        os.replace(part, dest)
    except DriverError:
        raise
    except (OSError, ValueError) as e:
        raise DriverError(f'download failed: {e}') from e
    finally:
        with contextlib.suppress(OSError):
            os.remove(part)
    return dest


# --------------------------------------------------------------------------------- extraction
def _is_driver_path(parts):
    return any(p.lower() in DRIVER_DIR_NAMES for p in parts[:-1])


def safe_join(root, name):
    """Join an archive member name below root; raises DriverError for absolute paths / traversal."""
    n = name.replace('\\', '/')
    parts = [p for p in n.split('/') if p not in ('', '.')]
    if (not parts or n.startswith('/') or re.match(r'^[A-Za-z]:', n)
            or any(p == '..' or ':' in p for p in parts)):
        raise DriverError(f'unsafe path in archive: {name!r}')
    root = os.path.abspath(root)
    target = os.path.abspath(os.path.join(root, *parts))
    if os.path.commonpath([root, target]) != root:
        raise DriverError(f'unsafe path in archive: {name!r}')
    return target


def _extract_zip(archive, outdir):
    total = 0
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as e:
        raise DriverError(f'archive is damaged: {e}') from e
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            target = safe_join(outdir, info.filename)       # validates every member, not only wanted ones
            parts = [p for p in info.filename.replace('\\', '/').split('/') if p]
            if not _is_driver_path(parts):
                continue
            total += info.file_size
            if total > MAX_UNPACKED:
                raise DriverError('archive is unexpectedly large')
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, 'wb') as dst:
                shutil.copyfileobj(src, dst)


def _extract_inno(archive, outdir):
    try:
        setup = inno.InnoSetup(archive)
        wanted = [f for f in setup.files if _is_driver_path(inno.safe_relpath(f.dest))]
        if not wanted:
            raise DriverError('the installer contains no driver folder')
        setup.extract(wanted, outdir)
    except inno.InnoError as e:
        raise DriverError(f'could not unpack the installer: {e}') from e


def find_inf(root, name=INF_NAME):
    """Locate `name` below root, preferring a path below a drivers/treiber folder."""
    hits = []
    for dp, _dn, fn in os.walk(root):
        for f in fn:
            if f.lower() == name.lower():
                hits.append(os.path.join(dp, f))
    if not hits:
        return None
    hits.sort(key=lambda p: (not _is_driver_path(os.path.relpath(p, root).replace('\\', '/').split('/')), len(p)))
    return hits[0]


def read_inf_text(path):
    with open(path, 'rb') as f:
        raw = f.read()
    for enc in ('utf-16', 'utf-8-sig', 'cp1252'):
        if enc == 'utf-16' and not raw.startswith((b'\xff\xfe', b'\xfe\xff')):
            continue
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('latin-1')


def inf_source_files(text):
    """Relative paths ('libusbK/amd64/libusbK.sys') named by the [SourceDisksFiles*] sections of an INF."""
    out, active = [], False
    for line in text.splitlines():
        line = line.split(';', 1)[0].strip()
        if line.startswith('['):
            active = line.lower().startswith('[sourcedisksfiles')
            continue
        if active and '=' in line:
            name, rhs = (s.strip() for s in line.split('=', 1))
            sub = rhs.split(',')[1].strip() if ',' in rhs else ''
            out.append((sub.replace('\\', '/').strip('/') + '/' + name.strip('"')).lstrip('/'))
    return out


def stage_inf(inf):
    """The vendor INF lives in drivers/inf/ but names its binaries relative to drivers/ (the vendor's own
    installer copies it one level up). Make sure the INF sits where its files are; returns the usable path."""
    files = inf_source_files(read_inf_text(inf))
    if not files:
        return inf
    d = os.path.dirname(inf)
    for _ in range(3):
        if all(os.path.isfile(os.path.join(d, *f.split('/'))) for f in files if f.split('/')[-1].lower().endswith(('.sys', '.dll'))):
            break
        d = os.path.dirname(d)
    else:
        raise DriverError('driver files referenced by P2Pro.inf are missing from the download')
    target = os.path.join(d, os.path.basename(inf))
    if os.path.abspath(target) != os.path.abspath(inf):
        shutil.copyfile(inf, target)
    return target


def extract_driver(archive, workdir):
    """Unpack the driver folder of the downloaded installer into workdir; returns the path of a usable P2Pro.inf."""
    with open(archive, 'rb') as f:
        magic = f.read(4)
    outdir = os.path.join(workdir, 'unpacked')
    os.makedirs(outdir, exist_ok=True)
    if magic[:2] == b'PK':
        _extract_zip(archive, outdir)
    elif magic[:2] == b'MZ':
        _extract_inno(archive, outdir)
    else:
        raise DriverError('unknown download format (expected an installer .exe or a zip)')
    inf = find_inf(outdir)
    if not inf:
        raise DriverError(f'{INF_NAME} not found in the download')
    return stage_inf(inf)


@contextlib.contextmanager
def workdir():
    d = tempfile.mkdtemp(prefix='p2pro_driver_')
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------------- installation
def pnputil_message(code):
    if code == PNPUTIL_OK:
        return 'Driver installed.'
    if code == PNPUTIL_REBOOT:
        return 'Driver installed. Windows asks for a restart to finish.'
    if code == PNPUTIL_NO_MORE_ITEMS:
        return 'Driver added to the driver store (no matching device is connected right now, or it is already up to date).'
    if code & 0xFFFFFFFF in (0xE000022F, 0xE0000247, 0x800B0100, 0x800B0109):
        return 'Windows rejected the driver package because it is not signed (no catalog file).'
    return f'pnputil failed with exit code {code & 0xFFFFFFFF:#x}.'


def pnputil_ok(code):
    return code in (PNPUTIL_OK, PNPUTIL_REBOOT, PNPUTIL_NO_MORE_ITEMS)


def run_elevated(exe, params='', cwd=None, show=0, timeout=900):
    """Run exe with the UAC 'runas' verb and wait; returns the exit code. show: 0 = hidden, 1 = normal."""
    if not is_windows():
        raise DriverError('driver installation is only available on Windows')
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('fMask', wintypes.ULONG), ('hwnd', wintypes.HWND),
                    ('lpVerb', wintypes.LPCWSTR), ('lpFile', wintypes.LPCWSTR), ('lpParameters', wintypes.LPCWSTR),
                    ('lpDirectory', wintypes.LPCWSTR), ('nShow', ctypes.c_int), ('hInstApp', wintypes.HINSTANCE),
                    ('lpIDList', ctypes.c_void_p), ('lpClass', wintypes.LPCWSTR), ('hkeyClass', wintypes.HKEY),
                    ('dwHotKey', wintypes.DWORD), ('hIconOrMonitor', wintypes.HANDLE), ('hProcess', wintypes.HANDLE)]

    SEE_MASK_NOCLOSEPROCESS = 0x40
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = 'runas'
    info.lpFile = exe
    info.lpParameters = params
    info.lpDirectory = cwd
    info.nShow = show
    k32 = ctypes.windll.kernel32
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.GetLastError()
        if err == ERROR_CANCELLED:
            raise DriverCancelled('Administrator permission was denied - the driver was not installed.')
        raise DriverError(f'could not start {os.path.basename(exe)} as administrator (error {err})')
    h = info.hProcess
    if not h:
        return 0
    try:
        if k32.WaitForSingleObject(h, int(timeout * 1000)) != 0:
            raise DriverError('the installer did not finish in time')
        code = wintypes.DWORD()
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        return code.value
    finally:
        k32.CloseHandle(h)


def _pnputil_exe():
    root = os.environ.get('SystemRoot', r'C:\Windows')
    for sub in ('System32', 'Sysnative'):
        p = os.path.join(root, sub, 'pnputil.exe')
        if os.path.isfile(p):
            return p
    return 'pnputil.exe'


def install_driver(inf_path):
    """Add the driver package to the driver store and install it on matching devices (pnputil, elevated).
    Returns pnputil's exit code (see pnputil_ok / pnputil_message). Raises DriverCancelled if UAC was declined."""
    if not os.path.isfile(inf_path):
        raise DriverError('driver file missing')
    return run_elevated(_pnputil_exe(), f'/add-driver "{inf_path}" /install', cwd=os.path.dirname(inf_path), show=0)


def install_vendor_fallback(inf_path):
    """Run the vendor's driver_install.exe elevated; returns True if it was started. Only call this after the user
    has been told what it does and agreed (see install_from_download's confirm_fallback).

    The package has no signed catalog (CatalogFile=P2Pro.cat is not shipped), so pnputil is expected to fail on
    current Windows. According to static analysis of the vendor's tools (nothing was run): driver_install.exe runs
    dpscat.exe, which creates a self-signed certificate, adds it to the machine's 'Root' (Trusted Root) and
    'TrustedPublisher' stores under the friendly name "libwdi", deletes the private key and never removes the
    certificate; dpinst64.exe then installs the driver. It first copies every INF from the package's inf folder,
    so (inferred) drivers for other camera models may be installed as well. The certificate stays trusted until
    it is removed by hand (certlm.msc)."""
    d = os.path.dirname(inf_path)
    for _ in range(3):
        exe = os.path.join(d, 'driver_install.exe')
        if os.path.isfile(exe):
            run_elevated(exe, '', cwd=d, show=1, timeout=300)
            return True
        d = os.path.dirname(d)
    return False


def install_from_download(status=None, progress=None, cancel=None, confirm_fallback=None):
    """Whole pipeline: find link, download, unpack, install. Returns a message for the user.
    status(text) / progress(done, total) are called from the calling thread. Raises DriverError.
    confirm_fallback() -> bool is called (from the calling thread) when pnputil has failed and the only way on is
    the vendor's installer, which adds a certificate to the machine. Without a callback, or if it returns False,
    that installer is not run (fail closed)."""
    say = status or (lambda s: None)
    with workdir() as wd:
        say('Looking up the driver download...')
        url = fetch_download_url(log=say)
        say('Downloading the Thermal Master package...')
        archive = download(url, os.path.join(wd, 'package.bin'), progress, cancel)
        say('Unpacking the driver...')
        inf = extract_driver(archive, wd)
        say('Installing the driver (confirm the administrator prompt)...')
        code = install_driver(inf)
        if pnputil_ok(code):
            return pnputil_message(code) + ' Unplug and replug the camera.'
        msg = pnputil_message(code)
        say('Windows rejected the unsigned driver package.')
        allowed = False
        if confirm_fallback is not None:
            try:
                allowed = bool(confirm_fallback())
            except Exception:
                allowed = False
        if not allowed:
            raise DriverError(msg + ' You chose not to run the vendor installer, so nothing was installed and no '
                              'certificate was added. "Try Windows Driver" needs no certificate.')
        say('Running the vendor installer (confirm the administrator prompt)...')
        started = install_vendor_fallback(inf)          # DriverCancelled (UAC declined) propagates
        if started:
            for _ in range(15):                         # the helper returns before the install is finished
                if is_driver_installed():
                    return 'Driver installed (vendor installer). Unplug and replug the camera.'
                time.sleep(2)
        raise DriverError(msg + ' The vendor installer was run, but the driver was not detected afterwards. It may '
                          'have added a certificate named "libwdi" to the Trusted Root and Trusted Publishers '
                          'stores (remove it with certlm.msc). "Try Windows Driver" needs no certificate.')


# --------------------------------------------------------------------------------- inbox WinUSB (no download)
class DriverCameraAbsent(DriverError):
    """The camera isn't plugged in."""


class DriverNeedsConfirmation(DriverError):
    """The camera is on the vendor's libusbK driver; replacing it must be confirmed by the user."""


def binding_state():
    """See winusb.binding_state: 'winusb' / 'libusbk' / 'usbccgp' / other service / 'absent' / 'unknown'."""
    return winusb.binding_state()


def fallback_text():
    return winusb.fallback_text()


def log_path():
    return winusb.default_log_path()


def _elevated_winusb(action, replace_libusbk=False, timeout=300):
    """Run this program again, elevated, with a winusb flag (handled at the top of app.py before Qt starts).
    Returns the result dict that the elevated run wrote; raises DriverCancelled / DriverError."""
    if not is_windows():
        raise DriverError('this is only available on Windows')
    with workdir() as wd:
        result = os.path.join(wd, 'result.json')
        args = winusb.build_args(action, result, winusb.default_log_path(), replace_libusbk)
        if getattr(sys, 'frozen', False):
            exe, params = sys.executable, subprocess.list2cmdline(args)
        else:
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.py')
            exe, params = sys.executable, subprocess.list2cmdline([script] + args)
        code = run_elevated(exe, params, cwd=winusb.system_directory(), show=0, timeout=timeout)
        res = winusb.read_result(result)
    if res is None:
        raise DriverError(f'The administrator step ended without a result (exit code {code}). '
                          f'Details: {winusb.default_log_path()}')
    if not res.get('ok'):
        if res.get('call') == winusb.CALL_ABSENT:
            raise DriverCameraAbsent(res.get('message') or 'The camera is not connected. Plug it in first.')
        raise DriverError(res.get('message') or f'failed (exit code {code}). Details: {winusb.default_log_path()}')
    return res


def install_inbox_winusb(status=None, replace_libusbk=False):
    """Bind the connected camera to the WinUSB driver that ships with Windows. Returns a message for the user.
    Raises DriverNeedsConfirmation (on libusbK without replace_libusbk), DriverCancelled (UAC declined), DriverError."""
    say = status or (lambda s: None)
    state = binding_state()
    if state == 'winusb' and winusb.app_guid_present():
        return 'The camera already uses the Windows driver (WinUSB).'
    if state == 'absent':
        raise DriverCameraAbsent('The camera is not connected. Plug it in first, then try again.')
    if state == 'libusbk' and not replace_libusbk:
        raise DriverNeedsConfirmation('The camera uses the vendor driver (libusbK). Replacing it needs confirmation.')
    say('Waiting for administrator permission...')
    res = _elevated_winusb('bind', replace_libusbk)
    msg = res.get('message') or 'Driver set.'
    return msg if 'already uses' in msg else msg + ' Unplug and replug the camera.'


def remove_inbox_winusb(status=None):
    """Undo the binding: remove the camera's device node (pnputil, elevated) so Windows chooses a driver again."""
    say = status or (lambda s: None)
    say('Waiting for administrator permission...')
    res = _elevated_winusb('revert')
    return res.get('message') or 'The Windows driver binding was removed. Unplug and replug the camera.'
