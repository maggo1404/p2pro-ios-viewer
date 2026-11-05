"""Bind the camera to the WinUSB driver that ships with Windows (no Qt imports, importable on any OS).

The P2 Pro (USB 0BDA:5840, device class EF/02/01, two interfaces) normally runs on usbccgp.sys, the generic
composite-device driver. This module does what Device Manager > Update driver > Let me pick > "WinUsb Device" does,
through SetupAPI: it makes the parent device node use the inbox `%SystemRoot%\\INF\\winusb.inf`. No file is
downloaded or installed and nothing is added to the driver store. Binding needs administrator rights (the caller
re-launches this program elevated with `--winusb-bind`, see run_cli); detection and diagnosis do not.

Everything that talks to Windows is in SetupApi and loaded lazily. The rest (hardware-ID matching, multi-sz
encoding, driver-node selection, state mapping, error texts, argument parsing, the bind/revert orchestration
against an api object) is plain Python and tested on Linux. Binding the camera this way has been confirmed by a
live test on Windows. Each SetupApi call is logged to `winusb_setup.log` for diagnosis (python winusb.py --diagnose).

Sources: tmp/report_winusb.md (call sequence); struct layouts from setupapi.h (x64 packing = natural alignment).
"""
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from collections import namedtuple

VID_PID = 'VID_0BDA&PID_5840'
CAMERA_HWID = 'USB\\' + VID_PID                 # the parent node; its children are USB\VID_..&PID_..&MI_00 / &MI_01
WINUSB_COMPAT_ID = 'USB\\MS_COMP_WINUSB'        # hardware ID of the "WinUsb Device" model in winusb.inf
# Generated once for this app (uuid4). Never reuse the vendor's GUID.
APP_INTERFACE_GUID = '{344460f2-f9e1-4626-9a6d-9b73c455567d}'
GUID_VALUE = 'DeviceInterfaceGUIDs'
LOG_NAME = 'winusb_setup.log'

# SetupAPI / registry constants (values from setupapi.h / winreg.h, see the report)
DIGCF_PRESENT, DIGCF_ALLCLASSES = 0x2, 0x4
SPDRP_HARDWAREID, SPDRP_SERVICE, SPDRP_CLASSGUID, SPDRP_INSTALL_STATE = 1, 4, 8, 0x22
DI_ENUMSINGLEINF, DI_FLAGSEX_ALLOWEXCLUDEDDRVS = 0x10000, 0x800
SPDIT_CLASSDRIVER = 1
DIOD_INHERIT_CLASSDRVS = 0x2
DIF_INSTALLDEVICE, DIF_INSTALLINTERFACES, DIF_REGISTER_COINSTALLERS = 0x02, 0x20, 0x22
DICS_FLAG_GLOBAL, DIREG_DEV = 1, 1
KEY_READ, KEY_WRITE = 0x20019, 0x20006
REG_SZ, REG_MULTI_SZ = 1, 7
ERROR_ACCESS_DENIED, ERROR_INVALID_PARAMETER, ERROR_INSUFFICIENT_BUFFER = 5, 87, 122
ERROR_NO_MORE_ITEMS, ERROR_CANCELLED, ERROR_INVALID_USER_BUFFER = 259, 1223, 1784
CREATE_NO_WINDOW = 0x08000000
PNPUTIL_OK_CODES = (0, 3010)                    # success / success, restart required

EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2
CALL_ABSENT = 'camera_absent'                   # WinUsbError.call when the camera isn't plugged in
VERIFY_TRIES = 60                               # x 0.5 s: how long to wait for the service to read WinUSB
RESULT_NAME = 'result.json'

MANUAL_STEPS = ('Device Manager > right-click the camera (under "Universal Serial Bus controllers" or '
                '"Universal Serial Bus devices") > Update driver > Browse my computer for drivers > '
                'Let me pick from a list of available drivers on my computer > Universal Serial Bus devices > '
                'WinUsb Device')


# --------------------------------------------------------------------------------- errors, log
class WinUsbError(Exception):
    """Failure with a message that can be shown to the user as is. `call` names the failing Windows call."""

    def __init__(self, message, call='', code=0, log_path=''):
        super().__init__(message)
        self.call, self.code, self.log_path = call, code, log_path


class NeedsConfirmation(WinUsbError):
    """The camera is on the vendor's libusbK driver and the user hasn't agreed to replace it."""


def describe_error(code):
    code &= 0xFFFFFFFF
    known = {ERROR_ACCESS_DENIED: 'access denied (administrator rights are required)',
             2: 'file not found', 3: 'path not found', 13: 'the data is invalid',
             ERROR_INVALID_PARAMETER: 'invalid parameter',
             ERROR_INSUFFICIENT_BUFFER: 'buffer too small', ERROR_NO_MORE_ITEMS: 'no more items',
             ERROR_CANCELLED: 'cancelled', 1168: 'element not found',
             ERROR_INVALID_USER_BUFFER: 'invalid structure size (cbSize)',
             0xE000022F: 'the driver package is not signed', 0xE0000247: 'the driver package is not signed',
             0x800B0100: 'the driver package is not signed', 0x800B0109: 'the driver package is not signed'}
    return known.get(code, 'Windows error') + f' ({code:#x})'


def default_log_path():
    return os.path.join(tempfile.gettempdir(), LOG_NAME)


class Log:
    """Append-only text log; never raises (logging must not break the install)."""

    def __init__(self, path=None):
        self.path = path or default_log_path()

    def write(self, text):
        try:
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(time.strftime('%H:%M:%S ') + text + '\n')
        except OSError:
            pass


def fail(log, call, code, extra=''):
    """Log and build the error for a failed Windows call."""
    log.write(f'{call} FAILED: {describe_error(code)}' + (f' {extra}' if extra else ''))
    return WinUsbError(f'{call} failed: {describe_error(code)}. Details: {log.path}', call, code, log.path)


# --------------------------------------------------------------------------------- pure logic
def is_camera_parent(hwids):
    """True for the parent node (hardware-ID list contains exactly USB\\VID_0BDA&PID_5840), not the &MI_xx children."""
    return any(h.strip().upper() == CAMERA_HWID for h in hwids)


def encode_multi_sz(strings):
    """REG_MULTI_SZ as UTF-16LE: each string and the list terminator carry a NUL, both are counted."""
    return b''.join(s.encode('utf-16-le') + b'\0\0' for s in strings) + b'\0\0'


def parse_multi_sz(data):
    """Decode a UTF-16LE multi-sz buffer; stops at the empty string (a missing final NUL is tolerated)."""
    text = bytes(data[:len(data) // 2 * 2]).decode('utf-16-le', errors='replace')
    out = []
    for s in text.split('\0'):
        if not s:
            break
        out.append(s)
    return out


def decode_sz(data):
    return bytes(data[:len(data) // 2 * 2]).decode('utf-16-le', errors='replace').split('\0', 1)[0]


def pick_winusb_driver(hwid_lists):
    """Index of the driver node whose hardware/compatible-ID list has USB\\MS_COMP_WINUSB, else None.
    (Selection by ID, not by the localizable description "WinUsb Device".)"""
    want = WINUSB_COMPAT_ID.upper()
    for i, ids in enumerate(hwid_lists):
        if any(h.strip().upper() == want for h in ids):
            return i
    return None


def state_from_service(service):
    """'winusb' / 'libusbk' / 'usbccgp' / other lower-cased service name / 'nodriver' (present, no service)."""
    s = (service or '').strip().lower()
    return s or 'nodriver'


def state_of(node):
    return 'absent' if node is None else state_from_service(node.service)


def is_wow64(env=None):
    """True for a 32-bit process on 64-bit Windows (SetupAPI refuses to install there: ERROR_IN_WOW64)."""
    env = os.environ if env is None else env
    return ctypes.sizeof(ctypes.c_void_p) == 4 and bool(env.get('PROCESSOR_ARCHITEW6432'))


def inbox_inf_path(env=None):
    env = os.environ if env is None else env
    return os.path.join(env.get('SystemRoot', r'C:\Windows'), 'INF', 'winusb.inf')


def fallback_text():
    return ('You can do the same by hand: ' + MANUAL_STEPS + '. Or use "Download Vendor Driver…" instead.')


CameraNode = namedtuple('CameraNode', 'instance_id service class_guid install_state')


# --------------------------------------------------------------------------------- ctypes structs
# Explicit fixed-size types (not wintypes) so the sizes can be checked on 64-bit Linux. WCHAR = c_uint16.
LINE_LEN, MAX_PATH = 256, 260


class GUID(ctypes.Structure):
    _fields_ = [('Data1', ctypes.c_uint32), ('Data2', ctypes.c_uint16), ('Data3', ctypes.c_uint16),
                ('Data4', ctypes.c_uint8 * 8)]


class FILETIME(ctypes.Structure):
    _fields_ = [('dwLowDateTime', ctypes.c_uint32), ('dwHighDateTime', ctypes.c_uint32)]


class SP_DEVINFO_DATA(ctypes.Structure):                    # 32 bytes on x64
    _fields_ = [('cbSize', ctypes.c_uint32), ('ClassGuid', GUID), ('DevInst', ctypes.c_uint32),
                ('Reserved', ctypes.c_size_t)]


class SP_DEVINSTALL_PARAMS_W(ctypes.Structure):             # 584 bytes on x64
    _fields_ = [('cbSize', ctypes.c_uint32), ('Flags', ctypes.c_uint32), ('FlagsEx', ctypes.c_uint32),
                ('hwndParent', ctypes.c_void_p), ('InstallMsgHandler', ctypes.c_void_p),
                ('InstallMsgHandlerContext', ctypes.c_void_p), ('FileQueue', ctypes.c_void_p),
                ('ClassInstallReserved', ctypes.c_size_t), ('Reserved', ctypes.c_uint32),
                ('DriverPath', ctypes.c_uint16 * MAX_PATH)]


class SP_DRVINFO_DATA_W(ctypes.Structure):                  # 1568 bytes on x64
    _fields_ = [('cbSize', ctypes.c_uint32), ('DriverType', ctypes.c_uint32), ('Reserved', ctypes.c_size_t),
                ('Description', ctypes.c_uint16 * LINE_LEN), ('MfgName', ctypes.c_uint16 * LINE_LEN),
                ('ProviderName', ctypes.c_uint16 * LINE_LEN), ('DriverDate', FILETIME),
                ('DriverVersion', ctypes.c_uint64)]


class SP_DRVINFO_DETAIL_DATA_W(ctypes.Structure):
    """setupapi.h: DWORD cbSize; FILETIME InfDate; DWORD CompatIDsOffset; DWORD CompatIDsLength;
    ULONG_PTR Reserved; WCHAR SectionName[LINE_LEN], InfFileName[MAX_PATH], DrvDescription[LINE_LEN];
    WCHAR HardwareID[ANYSIZE_ARRAY]. On x64 (natural alignment; derived by hand and checked against ctypes, not
    against a Windows compiler): Reserved at 24, SectionName at 32, HardwareID at 32+512+520+512 = 1576, sizeof =
    1578 rounded up to 8 = 1584. The variable-length ID list (hardware IDs, then compatible IDs, one multi-sz)
    continues past the end, so a larger buffer is passed and the list is parsed from offset 1576."""
    _fields_ = [('cbSize', ctypes.c_uint32), ('InfDate', FILETIME), ('CompatIDsOffset', ctypes.c_uint32),
                ('CompatIDsLength', ctypes.c_uint32), ('Reserved', ctypes.c_size_t),
                ('SectionName', ctypes.c_uint16 * LINE_LEN), ('InfFileName', ctypes.c_uint16 * MAX_PATH),
                ('DrvDescription', ctypes.c_uint16 * LINE_LEN), ('HardwareID', ctypes.c_uint16 * 1)]


# cbSize is UNVERIFIED: C callers pass sizeof (DETAIL_SIZEOF, 1584 on x64); the doc's "fixed part" would be the
# offset of HardwareID (DETAIL_FIXED, 1576). SetupApi._driver_hwids tries sizeof first, the other on ERROR_INVALID_USER_BUFFER.
DETAIL_FIXED = SP_DRVINFO_DETAIL_DATA_W.HardwareID.offset
DETAIL_SIZEOF = ctypes.sizeof(SP_DRVINFO_DETAIL_DATA_W)


def set_wchars(array, text):
    """Store text (NUL-terminated) into a c_uint16 array."""
    data = text.encode('utf-16-le')
    n = len(data) // 2
    if n >= len(array):
        raise ValueError('string too long for the buffer')
    for i in range(n):
        array[i] = data[2 * i] | (data[2 * i + 1] << 8)
    array[n] = 0


def get_wchars(array):
    return decode_sz(b''.join(int(c).to_bytes(2, 'little') for c in array))


def make_struct(cls):
    s = cls()
    s.cbSize = ctypes.sizeof(cls)
    return s


# --------------------------------------------------------------------------------- Windows API
class SetupApi:
    """Thin SetupAPI/registry wrapper. Every call is logged by name with its result or error code, a failure
    raises WinUsbError naming the call. Only constructed on Windows."""

    def __init__(self, log):
        self.log = log
        self.invalid = ctypes.c_void_p(-1).value            # INVALID_HANDLE_VALUE at the process's pointer width
        wd = ctypes.WinDLL                                  # AttributeError on non-Windows: callers check first
        self.sp = wd('setupapi', use_last_error=True)
        self.nd = wd('newdev', use_last_error=True)
        self.adv = wd('advapi32', use_last_error=True)
        self._declare()

    def _declare(self):
        P, V, U32, I = ctypes.POINTER, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int
        dev, drv, prm = P(SP_DEVINFO_DATA), P(SP_DRVINFO_DATA_W), P(SP_DEVINSTALL_PARAMS_W)
        W = ctypes.c_wchar_p

        def d(fn, args, res):
            fn.argtypes, fn.restype = args, res
        sp, adv = self.sp, self.adv
        d(sp.SetupDiGetClassDevsW, [V, W, V, U32], V)
        d(sp.SetupDiDestroyDeviceInfoList, [V], I)
        d(sp.SetupDiEnumDeviceInfo, [V, U32, dev], I)
        d(sp.SetupDiGetDeviceRegistryPropertyW, [V, dev, U32, P(U32), V, U32, P(U32)], I)
        d(sp.SetupDiGetDeviceInstanceIdW, [V, dev, V, U32, P(U32)], I)
        d(sp.SetupDiCreateDeviceInfoList, [V, V], V)
        d(sp.SetupDiOpenDeviceInfoW, [V, W, V, U32, dev], I)
        d(sp.SetupDiGetDeviceInstallParamsW, [V, dev, prm], I)
        d(sp.SetupDiSetDeviceInstallParamsW, [V, dev, prm], I)
        d(sp.SetupDiBuildDriverInfoList, [V, dev, U32], I)
        d(sp.SetupDiEnumDriverInfoW, [V, dev, U32, U32, drv], I)
        d(sp.SetupDiGetDriverInfoDetailW, [V, dev, drv, V, U32, P(U32)], I)
        d(sp.SetupDiSetSelectedDriverW, [V, dev, drv], I)
        d(sp.SetupDiCallClassInstaller, [U32, V, dev], I)
        d(sp.SetupDiOpenDevRegKey, [V, dev, U32, U32, U32, U32], V)
        d(sp.SetupDiCreateDevRegKeyW, [V, dev, U32, U32, U32, V, W], V)
        d(self.nd.DiInstallDevice, [V, V, dev, drv, U32, P(I)], I)
        d(adv.RegSetValueExW, [V, W, U32, U32, V, U32], ctypes.c_int32)
        d(adv.RegQueryValueExW, [V, W, V, P(U32), V, P(U32)], ctypes.c_int32)
        d(adv.RegCloseKey, [V], ctypes.c_int32)

    # -- helpers
    def _ok(self, name, ret, detail=''):
        """Log `name` with its result; True if the BOOL result is non-zero. Error code stays in last_error."""
        if ret:
            self.log.write(f'{name} ok' + (f' {detail}' if detail else ''))
            return True
        return False

    def _err(self):
        return ctypes.get_last_error()

    def _raise(self, name, extra=''):
        return fail(self.log, name, self._err(), extra)

    def _handle_ok(self, h):
        return h is not None and h != self.invalid

    def _open_set(self):
        hs = self.sp.SetupDiCreateDeviceInfoList(None, None)
        if not self._handle_ok(hs):
            raise self._raise('SetupDiCreateDeviceInfoList')
        return hs

    def _destroy(self, hs):
        if self._handle_ok(hs):
            self.sp.SetupDiDestroyDeviceInfoList(hs)

    def _open_instance(self, hs, instance_id):
        dev = make_struct(SP_DEVINFO_DATA)
        if not self.sp.SetupDiOpenDeviceInfoW(hs, instance_id, None, 0, ctypes.byref(dev)):
            raise self._raise('SetupDiOpenDeviceInfoW', instance_id)
        return dev

    def _property(self, hs, dev, prop):
        """(reg_type, bytes) of a device registry property, or None if it isn't set."""
        size = 2048
        for _ in range(2):
            buf = ctypes.create_string_buffer(size)
            typ, need = ctypes.c_uint32(), ctypes.c_uint32()
            if self.sp.SetupDiGetDeviceRegistryPropertyW(hs, ctypes.byref(dev), prop, ctypes.byref(typ), buf, size,
                                                         ctypes.byref(need)):
                return typ.value, buf.raw[:need.value]
            if self._err() != ERROR_INSUFFICIENT_BUFFER:
                return None
            size = need.value + 16
        return None

    def _instance_id(self, hs, dev):
        buf = ctypes.create_unicode_buffer(512)
        need = ctypes.c_uint32()
        if not self.sp.SetupDiGetDeviceInstanceIdW(hs, ctypes.byref(dev), buf, 512, ctypes.byref(need)):
            raise self._raise('SetupDiGetDeviceInstanceIdW')
        return buf.value

    # -- detection
    def find_camera(self, verbose=False):
        """CameraNode of the connected (DIGCF_PRESENT) parent device node, or None. Stale Enum entries are ignored."""
        hs = self.sp.SetupDiGetClassDevsW(None, 'USB', None, DIGCF_PRESENT | DIGCF_ALLCLASSES)
        if not self._handle_ok(hs):
            raise self._raise('SetupDiGetClassDevsW')
        try:
            i = 0
            while True:
                dev = make_struct(SP_DEVINFO_DATA)
                if not self.sp.SetupDiEnumDeviceInfo(hs, i, ctypes.byref(dev)):
                    if self._err() == ERROR_NO_MORE_ITEMS:
                        if verbose:
                            self.log.write('SetupDiEnumDeviceInfo: camera not among the connected USB devices')
                        return None
                    raise self._raise('SetupDiEnumDeviceInfo')
                i += 1
                p = self._property(hs, dev, SPDRP_HARDWAREID)
                if not p or not is_camera_parent(parse_multi_sz(p[1])):
                    continue
                inst = self._instance_id(hs, dev)
                svc = self._property(hs, dev, SPDRP_SERVICE)
                cls = self._property(hs, dev, SPDRP_CLASSGUID)
                ins = self._property(hs, dev, SPDRP_INSTALL_STATE)
                node = CameraNode(inst, decode_sz(svc[1]) if svc else '', decode_sz(cls[1]) if cls else '',
                                  int.from_bytes(ins[1][:4], 'little') if ins else None)
                if verbose:
                    self.log.write(f'camera found: {node}')
                return node
        finally:
            self._destroy(hs)

    # -- DeviceInterfaceGUIDs
    def _open_dev_key(self, hs, dev, create, sam):
        k = self.sp.SetupDiOpenDevRegKey(hs, ctypes.byref(dev), DICS_FLAG_GLOBAL, 0, DIREG_DEV, sam)
        if self._handle_ok(k):
            self.log.write('SetupDiOpenDevRegKey ok')
            return k
        err = self._err()
        self.log.write(f'SetupDiOpenDevRegKey failed: {describe_error(err)}')
        if not create:
            return None
        k = self.sp.SetupDiCreateDevRegKeyW(hs, ctypes.byref(dev), DICS_FLAG_GLOBAL, 0, DIREG_DEV, None, None)
        if not self._handle_ok(k):
            raise self._raise('SetupDiCreateDevRegKeyW')
        self.log.write('SetupDiCreateDevRegKeyW ok')
        return k

    def read_guids(self, instance_id):
        """DeviceInterfaceGUIDs of the device as a list, or None if the key/value doesn't exist."""
        hs = self._open_set()
        try:
            dev = self._open_instance(hs, instance_id)
            k = self._open_dev_key(hs, dev, False, KEY_READ)
            if k is None:
                return None
            try:
                size = ctypes.c_uint32(0)
                typ = ctypes.c_uint32()
                rc = self.adv.RegQueryValueExW(k, GUID_VALUE, None, ctypes.byref(typ), None, ctypes.byref(size))
                self.log.write(f'RegQueryValueExW({GUID_VALUE}) size query -> {rc}')
                if rc != 0:
                    return None
                buf = ctypes.create_string_buffer(size.value + 4)
                rc = self.adv.RegQueryValueExW(k, GUID_VALUE, None, ctypes.byref(typ), buf, ctypes.byref(size))
                self.log.write(f'RegQueryValueExW({GUID_VALUE}) -> {rc}')
                return parse_multi_sz(buf.raw[:size.value]) if rc == 0 and typ.value == REG_MULTI_SZ else None
            finally:
                self.adv.RegCloseKey(k)
        finally:
            self._destroy(hs)

    def write_guids(self, instance_id, guids=(APP_INTERFACE_GUID,)):
        hs = self._open_set()
        try:
            dev = self._open_instance(hs, instance_id)
            k = self._open_dev_key(hs, dev, True, KEY_READ | KEY_WRITE)
            try:
                data = encode_multi_sz(guids)
                buf = ctypes.create_string_buffer(data, len(data))
                rc = self.adv.RegSetValueExW(k, GUID_VALUE, 0, REG_MULTI_SZ, buf, len(data))
                if rc != 0:                                 # registry functions return the error, not last-error
                    raise fail(self.log, 'RegSetValueExW', rc)
                self.log.write(f'RegSetValueExW({GUID_VALUE}) ok')
            finally:
                self.adv.RegCloseKey(k)
        finally:
            self._destroy(hs)

    # -- binding
    def _driver_hwids(self, hs, dev, drv):
        """Hardware + compatible IDs of one driver node (SetupDiGetDriverInfoDetailW)."""
        attempts = [(DETAIL_SIZEOF, DETAIL_SIZEOF + 8192), (DETAIL_FIXED, DETAIL_SIZEOF + 8192)]
        last = 0
        for cb, size in attempts:
            for _ in range(2):
                buf = ctypes.create_string_buffer(size)
                head = SP_DRVINFO_DETAIL_DATA_W.from_buffer(buf)
                head.cbSize = cb
                need = ctypes.c_uint32()
                if self.sp.SetupDiGetDriverInfoDetailW(hs, ctypes.byref(dev), ctypes.byref(drv), buf, size,
                                                       ctypes.byref(need)):
                    return parse_multi_sz(buf.raw[DETAIL_FIXED:])
                last = self._err()
                if last != ERROR_INSUFFICIENT_BUFFER:
                    break
                size = max(need.value, size) + 64
            if last != ERROR_INVALID_USER_BUFFER:
                break                                       # only a rejected cbSize is worth retrying with the other
        raise fail(self.log, 'SetupDiGetDriverInfoDetailW', last)

    def _find_winusb_node(self, hs, dev):
        nodes, ids = [], []
        i = 0
        while True:
            drv = make_struct(SP_DRVINFO_DATA_W)
            if not self.sp.SetupDiEnumDriverInfoW(hs, ctypes.byref(dev), SPDIT_CLASSDRIVER, i, ctypes.byref(drv)):
                if self._err() == ERROR_NO_MORE_ITEMS:
                    break
                raise self._raise('SetupDiEnumDriverInfoW')
            ids.append(self._driver_hwids(hs, dev, drv))
            nodes.append(drv)
            i += 1
        self.log.write(f'SetupDiEnumDriverInfoW: {len(nodes)} driver node(s) in winusb.inf: {ids}')
        pick = pick_winusb_driver(ids)
        if pick is None:
            self.log.write(f'no driver node lists {WINUSB_COMPAT_ID}')
            raise WinUsbError(f'winusb.inf has no {WINUSB_COMPAT_ID} driver node (SetupDiEnumDriverInfoW). '
                              f'Details: {self.log.path}', 'SetupDiEnumDriverInfoW', 0, self.log.path)
        self.log.write(f'selected driver node {pick}: {get_wchars(nodes[pick].Description)!r}')
        return nodes[pick]

    def _dif_install(self, hs, dev):
        for code, name in ((DIF_REGISTER_COINSTALLERS, 'DIF_REGISTER_COINSTALLERS'),
                           (DIF_INSTALLINTERFACES, 'DIF_INSTALLINTERFACES'), (DIF_INSTALLDEVICE, 'DIF_INSTALLDEVICE')):
            if not self.sp.SetupDiCallClassInstaller(code, hs, ctypes.byref(dev)):
                raise self._raise(f'SetupDiCallClassInstaller({name})')
            self.log.write(f'SetupDiCallClassInstaller({name}) ok')

    def install_winusb(self, instance_id):
        """Make the device use the inbox winusb.inf (report section 1, steps 3-10). Returns True if Windows asks for a restart."""
        sp = self.sp
        hs = self._open_set()
        try:
            prm = make_struct(SP_DEVINSTALL_PARAMS_W)
            if not self._ok('SetupDiGetDeviceInstallParamsW', sp.SetupDiGetDeviceInstallParamsW(hs, None, ctypes.byref(prm))):
                raise self._raise('SetupDiGetDeviceInstallParamsW')
            prm.Flags |= DI_ENUMSINGLEINF
            prm.FlagsEx |= DI_FLAGSEX_ALLOWEXCLUDEDDRVS
            set_wchars(prm.DriverPath, inbox_inf_path())
            if not self._ok('SetupDiSetDeviceInstallParamsW', sp.SetupDiSetDeviceInstallParamsW(hs, None, ctypes.byref(prm))):
                raise self._raise('SetupDiSetDeviceInstallParamsW')
            if not self._ok('SetupDiBuildDriverInfoList', sp.SetupDiBuildDriverInfoList(hs, None, SPDIT_CLASSDRIVER)):
                raise self._raise('SetupDiBuildDriverInfoList', inbox_inf_path())
            dev = make_struct(SP_DEVINFO_DATA)
            if not self._ok('SetupDiOpenDeviceInfoW', sp.SetupDiOpenDeviceInfoW(
                    hs, instance_id, None, DIOD_INHERIT_CLASSDRVS, ctypes.byref(dev))):
                raise self._raise('SetupDiOpenDeviceInfoW', instance_id)
            drv = self._find_winusb_node(hs, dev)
            if not self._ok('SetupDiSetSelectedDriverW', sp.SetupDiSetSelectedDriverW(hs, ctypes.byref(dev), ctypes.byref(drv))):
                raise self._raise('SetupDiSetSelectedDriverW')
            reboot = ctypes.c_int(0)
            if self._ok('DiInstallDevice', self.nd.DiInstallDevice(None, hs, ctypes.byref(dev), ctypes.byref(drv), 0,
                                                                     ctypes.byref(reboot)), f'reboot={reboot.value}'):
                return bool(reboot.value)
            first = self._err()
            self.log.write(f'DiInstallDevice FAILED: {describe_error(first)}; trying the DIF sequence')
            try:
                self._dif_install(hs, dev)
            except WinUsbError as e:
                raise WinUsbError(f'DiInstallDevice failed: {describe_error(first)}; then {e.call} failed: '
                                  f'{describe_error(e.code)}. The camera may be without a working driver now: unplug '
                                  f'and replug it, which usually restores the previous driver. Details: {self.log.path}',
                                  'DiInstallDevice', first, self.log.path) from e
            return False
        finally:
            self._destroy(hs)


def load_api(log=None):
    """SetupApi for this process; raises WinUsbError when not on Windows."""
    if sys.platform != 'win32':
        raise WinUsbError('this works only on Windows')
    return SetupApi(log or Log())


# --------------------------------------------------------------------------------- orchestration (api-agnostic)
def ensure_guid(api, instance_id, log):
    """Make sure DeviceInterfaceGUIDs holds the app's GUID, keeping any GUIDs already there. True if it was written."""
    have = api.read_guids(instance_id) or []
    if APP_INTERFACE_GUID.lower() in [g.lower() for g in have]:
        return False
    log.write(f'DeviceInterfaceGUIDs lacks the app GUID (has {have}); writing')
    api.write_guids(instance_id, list(have) + [APP_INTERFACE_GUID])
    return True


def bind(api, log, replace_libusbk=False, sleep=time.sleep):
    """Bind the connected camera to the inbox WinUSB. api: SetupApi or a stand-in. Returns a message.
    DeviceInterfaceGUIDs is only written after a successful install, so a failed install leaves it untouched."""
    if is_wow64():
        raise WinUsbError('This is a 32-bit program on 64-bit Windows; Windows refuses driver changes from it '
                          '(ERROR_IN_WOW64). Use the 64-bit version.', 'IsWow64', 0, log.path)
    cam = api.find_camera(verbose=True)
    if cam is None:
        raise WinUsbError('The camera is not connected. Plug it in first, then try again.', CALL_ABSENT, 0, log.path)
    state = state_of(cam)
    log.write(f'bind: {cam.instance_id} service={cam.service!r} state={state}')
    if state == 'winusb':
        if ensure_guid(api, cam.instance_id, log):
            return ('The camera already used the Windows driver (WinUSB), but its interface registration was missing '
                    'and has been added.')
        return 'The camera already uses the Windows driver (WinUSB).'
    if state == 'libusbk' and not replace_libusbk:
        raise NeedsConfirmation('The camera uses the vendor driver (libusbK); replacing it needs confirmation.',
                                'find_camera', 0, log.path)
    reboot = api.install_winusb(cam.instance_id)
    after = None
    for _ in range(VERIFY_TRIES):                           # the device node is restarted by the install
        after = api.find_camera()
        if state_of(after) == 'winusb':
            break
        sleep(0.5)
    if state_of(after) != 'winusb':
        got = state_of(after)
        log.write(f'verify: service is {got!r} after the install (restart requested: {bool(reboot)})')
        if reboot:
            return 'Windows installed the driver but needs a restart to finish.'
        raise WinUsbError(f'Windows reported success, but the camera still reports driver "{got}". '
                          f'Details: {log.path}', 'verify', 0, log.path)
    ensure_guid(api, after.instance_id, log)
    return 'The camera now uses the Windows driver (WinUSB).'


def system_directory():
    """Windows' System32 directory from GetSystemDirectoryW (not from the environment)."""
    buf = ctypes.create_unicode_buffer(520)
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.GetSystemDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    k32.GetSystemDirectoryW.restype = ctypes.c_uint32
    n = k32.GetSystemDirectoryW(buf, 520)
    if not n or n >= 520:
        raise WinUsbError('GetSystemDirectoryW failed', 'GetSystemDirectoryW', ctypes.get_last_error())
    return buf.value


def pnputil_exe():
    """Full path of pnputil.exe; fails closed (no search by bare name)."""
    try:
        p = os.path.join(system_directory(), 'pnputil.exe')
    except (AttributeError, OSError) as e:
        raise WinUsbError(f'cannot locate pnputil.exe ({e})', 'pnputil_exe') from e
    if not os.path.isfile(p):
        raise WinUsbError(f'pnputil.exe not found at {p}', 'pnputil_exe')
    return p


def remove_device_supported():
    """pnputil /remove-device exists since Windows 10 1607 (build 14393)."""
    v = getattr(sys, 'getwindowsversion', None)
    return v is None or v().build >= 14393


def run_pnputil(args):
    kw = {'creationflags': CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    p = subprocess.run([pnputil_exe()] + args, capture_output=True, timeout=120, stdin=subprocess.DEVNULL, **kw)
    return p.returncode, (p.stdout + p.stderr).decode('cp850', errors='replace')


def revert(api, log, run=run_pnputil):
    """Remove the camera's device node (pnputil /remove-device) and rescan: Windows then picks the driver again.
    The inbox winusb.inf is never touched."""
    cam = api.find_camera(verbose=True)
    if cam is None:
        raise WinUsbError('The camera is not connected. Plug it in first, then try again.', CALL_ABSENT, 0, log.path)
    if not remove_device_supported():
        raise WinUsbError('Removing the binding needs Windows 10 version 1607 or later (pnputil /remove-device). '
                          'Use Device Manager > right-click the camera > Uninstall device, then replug it.',
                          'pnputil /remove-device', 0, log.path)
    if state_of(cam) != 'winusb':
        raise WinUsbError(f'The camera does not use WinUSB (driver: {state_of(cam)}); nothing to remove.',
                          'find_camera', 0, log.path)
    for args in (['/remove-device', cam.instance_id], ['/scan-devices']):
        rc, out = run(args)
        log.write(f'pnputil {" ".join(args)} -> {rc}: {out.strip()[:300]}')
        if rc not in PNPUTIL_OK_CODES:
            note = (' The device was removed, but the rescan failed: unplug and replug the camera.'
                    if args[0] == '/scan-devices' else '')
            raise WinUsbError(f'pnputil {args[0]} failed with exit code {rc & 0xFFFFFFFF:#x}.{note} '
                              f'Details: {log.path}', 'pnputil ' + args[0], rc, log.path)
    return 'The Windows driver binding was removed. Unplug and replug the camera.'


def binding_state():
    """Driver of the CONNECTED camera, unprivileged: 'winusb' / 'libusbk' / 'usbccgp' / other service name /
    'nodriver' / 'absent' (not connected, or not Windows) / 'unknown' (SetupAPI failed)."""
    if sys.platform != 'win32':
        return 'absent'
    try:
        return state_of(load_api(Log(os.devnull)).find_camera())
    except Exception:
        return 'unknown'


def app_guid_present():
    """Unprivileged: True/False if the connected camera's DeviceInterfaceGUIDs has the app GUID, None if unknown."""
    if sys.platform != 'win32':
        return None
    try:
        api = load_api(Log(os.devnull))
        cam = api.find_camera()
        if cam is None:
            return None
        return APP_INTERFACE_GUID.lower() in [g.lower() for g in (api.read_guids(cam.instance_id) or [])]
    except Exception:
        return None


# --------------------------------------------------------------------------------- diagnose
def _usbflags():
    import winreg
    out = []
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\usbflags') as k:
        i = 0
        while True:
            try:
                name = winreg.EnumKey(k, i)
            except OSError:
                break
            i += 1
            if name.upper().startswith('0BDA5840'):
                try:
                    with winreg.OpenKey(k, name) as sk:
                        val = winreg.QueryValueEx(sk, 'osvc')[0]
                    out.append(f'{name}\\osvc = {bytes(val).hex() if isinstance(val, (bytes, bytearray)) else val}')
                except OSError:
                    out.append(f'{name}\\osvc = (not present)')
    return out or ['no usbflags\\0BDA5840* key']


def libusb_version():
    class Version(ctypes.Structure):
        _fields_ = [('major', ctypes.c_uint16), ('minor', ctypes.c_uint16), ('micro', ctypes.c_uint16),
                    ('nano', ctypes.c_uint16), ('rc', ctypes.c_char_p), ('describe', ctypes.c_char_p)]
    import libusb_package
    be = libusb_package.get_libusb1_backend()
    fn = be.lib.libusb_get_version
    fn.restype = ctypes.POINTER(Version)
    v = fn().contents
    return f'{v.major}.{v.minor}.{v.micro}.{v.nano}{(v.rc or b"").decode()}'


def diagnose():
    """Multi-line report that the user can paste back."""
    lines = [f'python {sys.version.split()[0]}, {ctypes.sizeof(ctypes.c_void_p) * 8}-bit, platform {sys.platform}',
             f'wow64 (32-bit process on 64-bit Windows): {is_wow64()}', f'log file: {default_log_path()}']
    try:
        lines.append(f'libusb: {libusb_version()}')
    except Exception as e:
        lines.append(f'libusb: unavailable ({type(e).__name__}: {e})')
    if sys.platform != 'win32':
        return '\n'.join(lines + ['not Windows: no driver information'])
    try:
        api = load_api(Log(os.devnull))
        cam = api.find_camera()
        if cam is None:
            lines.append('camera: not connected')
        else:
            guids = api.read_guids(cam.instance_id)
            lines += [f'camera instance id: {cam.instance_id}', f'service: {cam.service or "(none)"}',
                      f'state: {state_of(cam)}', f'class GUID: {cam.class_guid or "(none)"}',
                      f'install state (0 = installed): {cam.install_state}',
                      f'{GUID_VALUE}: {guids if guids is not None else "(not present)"}',
                      f'app GUID present: {guids is not None and APP_INTERFACE_GUID.lower() in [g.lower() for g in guids]}']
    except Exception as e:
        lines.append(f'camera query failed: {type(e).__name__}: {e}')
    try:
        lines += _usbflags()
    except Exception as e:
        lines.append(f'usbflags: unavailable ({type(e).__name__}: {e})')
    return '\n'.join(lines)


# --------------------------------------------------------------------------------- command line (elevated runs)
ACTIONS = {'--winusb-bind': 'bind', '--winusb-revert': 'revert', '--winusb-diagnose': 'diagnose', '--diagnose': 'diagnose'}


def parse_args(argv):
    """None if argv has no winusb action. Else dict(action, result, log, replace_libusbk); ValueError on bad usage."""
    action = None
    out = {'result': None, 'log': None, 'replace_libusbk': False}
    i = 0
    argv = list(argv)
    while i < len(argv):
        a = argv[i]
        i += 1
        if a in ACTIONS:
            action = ACTIONS[a]
        elif a in ('--result', '--log'):
            if i >= len(argv):
                raise ValueError(f'{a} needs a path')
            out[a[2:]] = argv[i]
            i += 1
        elif a == '--replace-libusbk':
            out['replace_libusbk'] = True
    return None if action is None else dict(out, action=action)


def build_args(action, result=None, log=None, replace_libusbk=False):
    """Argument list for an elevated run (inverse of parse_args); quote with subprocess.list2cmdline."""
    flag = {v: k for k, v in ACTIONS.items() if k != '--diagnose'}[action]
    args = [flag]
    if result:
        args += ['--result', result]
    if log:
        args += ['--log', log]
    if replace_libusbk:
        args.append('--replace-libusbk')
    return args


def is_reparse(path):
    """True for a symlink / junction / other reparse point (lstat does not follow them)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return os.path.islink(path) or bool(getattr(st, 'st_file_attributes', 0) & 0x400)


def validate_output_paths(result, log, root=None):
    """The elevated run writes only fixed file names directly below the temp directory (or a fresh directory in it).
    Raises ValueError for anything else, before any file is opened."""
    root = os.path.realpath(root or tempfile.gettempdir())
    for path, name in ((result, RESULT_NAME), (log, LOG_NAME)):
        if path is None:
            continue
        if os.path.basename(path) != name:
            raise ValueError(f'unexpected file name {os.path.basename(path)!r} (want {name})')
        parent = os.path.realpath(os.path.dirname(os.path.abspath(path)))
        if parent != root and os.path.commonpath([parent, root]) != root:
            raise ValueError(f'{path} is not below {root}')
        if is_reparse(path) or is_reparse(os.path.dirname(os.path.abspath(path))):
            raise ValueError(f'{path} is a link')


def write_result(path, ok, message, call='', code=0, log=''):
    if not path:
        return
    try:
        with open(path, 'x', encoding='utf-8') as f:        # exclusive: never overwrite an existing file
            json.dump({'ok': ok, 'message': message, 'call': call, 'code': code, 'log': log}, f)
    except OSError:
        pass


def read_result(path):
    """Result dict written by an elevated run, or None if missing/unreadable."""
    try:
        with open(path, encoding='utf-8') as f:
            r = json.load(f)
        return r if isinstance(r, dict) and 'ok' in r else None
    except (OSError, ValueError):
        return None


def show_text(text):
    """Windowed (frozen) exe has no console: also show the text in a message box (Ctrl+C copies it)."""
    if sys.platform == 'win32' and getattr(sys, 'frozen', False):
        try:
            ctypes.windll.user32.MessageBoxW(None, text, 'P2 Pro WinUSB diagnosis', 0x40)
        except Exception:
            pass


def run_cli(argv, api_factory=load_api):
    """Handle the winusb flags. Returns an exit code, or None if argv is not for us (start the GUI)."""
    try:
        args = parse_args(argv)
    except ValueError as e:
        print(f'winusb: {e}', file=sys.stderr)
        return EXIT_USAGE
    if args is None:
        return None
    if args['action'] == 'diagnose':
        text = diagnose()
        path = os.path.join(tempfile.gettempdir(), 'winusb_diagnose.txt')
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text + '\n')
            text += f'\n(saved to {path})'
        except OSError:
            pass
        if sys.stdout:
            print(text)
        show_text(text)
        return EXIT_OK
    try:
        validate_output_paths(args['result'], args['log'])
    except ValueError as e:
        print(f'winusb: {e}', file=sys.stderr)
        return EXIT_USAGE
    log = Log(args['log'])
    log.write(f'--- {args["action"]} (pid {os.getpid()}, {ctypes.sizeof(ctypes.c_void_p) * 8}-bit)')
    try:
        api = api_factory(log)
        if args['action'] == 'bind':
            msg = bind(api, log, args['replace_libusbk'])
        else:
            msg = revert(api, log)
        log.write('done: ' + msg)
        write_result(args['result'], True, msg, log=log.path)
        return EXIT_OK
    except WinUsbError as e:
        log.write(f'error: {e}')
        write_result(args['result'], False, str(e), e.call, e.code, e.log_path or log.path)
        return EXIT_ERROR
    except Exception as e:
        log.write('unexpected error:\n' + traceback.format_exc())
        write_result(args['result'], False, f'{type(e).__name__}: {e}. Details: {log.path}', type(e).__name__, 0, log.path)
        return EXIT_ERROR


if __name__ == '__main__':
    rc = run_cli(sys.argv[1:])
    if rc is None:
        print('usage: python winusb.py --diagnose | --winusb-bind | --winusb-revert [--result FILE] [--log FILE]')
        rc = EXIT_USAGE
    sys.exit(rc)
