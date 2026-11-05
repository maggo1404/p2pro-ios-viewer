"""Run: python -m unittest discover tests. No Windows, no camera: struct layouts, pure logic and the bind/revert
orchestration against a fake api. The SetupAPI calls themselves (winusb.SetupApi) are not exercised here; binding has been confirmed by a live test."""
import ctypes
import json
import inspect
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import driver  # noqa: E402
import winusb  # noqa: E402

PARENT = 'USB\\VID_0BDA&PID_5840'
X64 = ctypes.sizeof(ctypes.c_void_p) == 8


@unittest.skipUnless(X64, 'struct sizes are the 64-bit ones')
class Structs(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(ctypes.sizeof(winusb.GUID), 16)
        self.assertEqual(ctypes.sizeof(winusb.SP_DEVINFO_DATA), 32)
        self.assertEqual(ctypes.sizeof(winusb.SP_DEVINSTALL_PARAMS_W), 584)
        self.assertEqual(ctypes.sizeof(winusb.SP_DRVINFO_DATA_W), 1568)

    def test_detail_layout(self):
        d = winusb.SP_DRVINFO_DETAIL_DATA_W
        self.assertEqual(d.CompatIDsOffset.offset, 12)
        self.assertEqual(d.Reserved.offset, 24)
        self.assertEqual(d.SectionName.offset, 32)
        self.assertEqual(d.HardwareID.offset, 1576)
        self.assertEqual(winusb.DETAIL_FIXED, 1576)
        self.assertEqual(winusb.DETAIL_SIZEOF, 1584)

    def test_field_offsets(self):
        self.assertEqual(winusb.SP_DRVINFO_DATA_W.DriverDate.offset, 1552 + 0)
        self.assertEqual(winusb.SP_DRVINFO_DATA_W.DriverVersion.offset, 1560)
        self.assertEqual(winusb.SP_DEVINSTALL_PARAMS_W.DriverPath.offset, 60 + 0)

    def test_wchar_helpers(self):
        arr = (ctypes.c_uint16 * 8)()
        winusb.set_wchars(arr, 'C:\\INF')
        self.assertEqual(winusb.get_wchars(arr), 'C:\\INF')
        with self.assertRaises(ValueError):
            winusb.set_wchars(arr, 'x' * 8)
        s = winusb.make_struct(winusb.SP_DEVINFO_DATA)
        self.assertEqual(s.cbSize, 32)


class PureLogic(unittest.TestCase):
    def test_parent_vs_children(self):
        self.assertTrue(winusb.is_camera_parent([PARENT, 'USB\\COMPOSITE'.lower()]))
        self.assertTrue(winusb.is_camera_parent(['usb\\vid_0bda&pid_5840']))
        self.assertFalse(winusb.is_camera_parent([PARENT + '&MI_00', 'USB\\VID_0BDA&PID_5840&REV_0002&MI_00']))
        self.assertFalse(winusb.is_camera_parent([PARENT + '&REV_0002', 'USB\\VID_0BDA&PID_5841']))
        self.assertFalse(winusb.is_camera_parent([]))

    def test_multi_sz_counts_both_nuls(self):
        g = winusb.APP_INTERFACE_GUID
        data = winusb.encode_multi_sz([g])
        self.assertEqual(len(data), (len(g) + 2) * 2)
        self.assertTrue(data.endswith(b'\0\0\0\0'))
        self.assertEqual(winusb.encode_multi_sz([]), b'\0\0')
        self.assertEqual(winusb.parse_multi_sz(data), [g])
        two = winusb.encode_multi_sz(['a', 'bc'])
        self.assertEqual(two, 'a\0bc\0\0'.encode('utf-16-le'))
        self.assertEqual(winusb.parse_multi_sz(two + b'\0\0junk'), ['a', 'bc'])
        self.assertEqual(winusb.parse_multi_sz('a\0b'.encode('utf-16-le')), ['a', 'b'])     # missing terminator

    def test_app_guid_is_not_the_vendor_one(self):
        import uuid
        uuid.UUID(winusb.APP_INTERFACE_GUID)
        self.assertTrue(winusb.APP_INTERFACE_GUID.startswith('{') and winusb.APP_INTERFACE_GUID.endswith('}'))

    def test_driver_node_selection_by_id(self):
        lists = [['USB\\VID_1234&PID_0001'], ['usb\\ms_comp_winusb', 'USB\\X'], ['USB\\MS_COMP_WINUSB']]
        self.assertEqual(winusb.pick_winusb_driver(lists), 1)
        self.assertIsNone(winusb.pick_winusb_driver([['USB\\MS_COMP_WINUSB_EXTRA'], []]))
        self.assertIsNone(winusb.pick_winusb_driver([]))

    def test_service_to_state(self):
        for svc, want in (('WinUSB', 'winusb'), ('libusbK', 'libusbk'), ('usbccgp', 'usbccgp'), ('Foo', 'foo'),
                          ('', 'nodriver'), (None, 'nodriver')):
            self.assertEqual(winusb.state_from_service(svc), want)
        self.assertEqual(winusb.state_of(None), 'absent')
        self.assertEqual(winusb.state_of(winusb.CameraNode('i', 'WinUSB', '', 0)), 'winusb')

    def test_error_messages(self):
        self.assertIn('access denied', winusb.describe_error(5))
        self.assertIn('0x5', winusb.describe_error(5))
        self.assertIn('not signed', winusb.describe_error(0xE000022F))
        self.assertIn('0x7b', winusb.describe_error(123))
        e = winusb.fail(winusb.Log(os.devnull), 'DiInstallDevice', 5)
        self.assertIn('DiInstallDevice', str(e))
        self.assertEqual((e.call, e.code), ('DiInstallDevice', 5))

    def test_wow64(self):
        with mock.patch.object(winusb.ctypes, 'sizeof', return_value=4):
            self.assertTrue(winusb.is_wow64({'PROCESSOR_ARCHITEW6432': 'AMD64'}))
            self.assertFalse(winusb.is_wow64({}))
        if X64:
            self.assertFalse(winusb.is_wow64({'PROCESSOR_ARCHITEW6432': 'AMD64'}))

    def test_inf_path(self):
        self.assertEqual(winusb.inbox_inf_path({'SystemRoot': 'C:\\W'}), os.path.join('C:\\W', 'INF', 'winusb.inf'))

    def test_manual_steps_text(self):
        t = winusb.fallback_text()
        for part in ('Update driver', 'Browse my computer', 'Let me pick', 'Universal Serial Bus devices', 'WinUsb Device',
                     'Download Vendor Driver'):
            self.assertIn(part, t)

    def test_non_windows_is_harmless(self):
        if sys.platform != 'win32':
            self.assertEqual(winusb.binding_state(), 'absent')
            self.assertIn('not Windows', winusb.diagnose())
            with self.assertRaises(winusb.WinUsbError):
                winusb.load_api()


class Args(unittest.TestCase):
    def test_none_for_gui_start(self):
        self.assertIsNone(winusb.parse_args([]))
        self.assertIsNone(winusb.parse_args(['--other']))

    def test_actions_and_paths(self):
        a = winusb.parse_args(['--winusb-bind', '--result', 'C:\\a b\\r.json', '--log', 'l.log', '--replace-libusbk'])
        self.assertEqual(a, {'action': 'bind', 'result': 'C:\\a b\\r.json', 'log': 'l.log', 'replace_libusbk': True})
        self.assertEqual(winusb.parse_args(['--winusb-revert'])['action'], 'revert')
        self.assertEqual(winusb.parse_args(['--winusb-diagnose'])['action'], 'diagnose')
        self.assertEqual(winusb.parse_args(['--diagnose'])['action'], 'diagnose')
        with self.assertRaises(ValueError):
            winusb.parse_args(['--winusb-bind', '--result'])

    def test_roundtrip_with_spaces_through_the_command_line(self):
        args = winusb.build_args('bind', 'C:\\Users\\A B\\res ult.json', 'C:\\Temp\\winusb_setup.log', True)
        line = subprocess.list2cmdline(['C:\\Program Files\\app.py'] + args)
        self.assertIn('"C:\\Users\\A B\\res ult.json"', line)
        back = winusb.parse_args(args)
        self.assertEqual(back['result'], 'C:\\Users\\A B\\res ult.json')
        self.assertTrue(back['replace_libusbk'])
        self.assertEqual(winusb.build_args('revert'), ['--winusb-revert'])

    def test_result_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'r.json')
            self.assertIsNone(winusb.read_result(p))
            winusb.write_result(p, False, 'boom', 'DiInstallDevice', 5, 'log')
            r = winusb.read_result(p)
            self.assertEqual((r['ok'], r['message'], r['call'], r['code']), (False, 'boom', 'DiInstallDevice', 5))
            with open(p, 'w') as f:
                f.write('not json')
            self.assertIsNone(winusb.read_result(p))


class PathChecks(unittest.TestCase):
    def test_accepts_fixed_names_below_temp(self):
        with tempfile.TemporaryDirectory() as d:
            winusb.validate_output_paths(os.path.join(d, 'result.json'), os.path.join(d, 'winusb_setup.log'))
            winusb.validate_output_paths(None, winusb.default_log_path())

    def test_rejects_other_names_and_places(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory(dir=os.path.expanduser('~')) as home:
            for res, log in ((os.path.join(d, 'x.json'), None), (None, os.path.join(d, 'other.log')),
                             (os.path.join(home, 'result.json'), None), (None, os.path.join(home, 'winusb_setup.log')),
                             (os.path.join(d, '..', '..', 'result.json'), None)):
                with self.assertRaises(ValueError, msg=(res, log)):
                    winusb.validate_output_paths(res, log, root=d)

    @unittest.skipIf(sys.platform == 'win32', 'symlinks need privileges on Windows')
    def test_rejects_links(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, 'victim')
            open(target, 'w').close()
            link = os.path.join(d, 'result.json')
            os.symlink(target, link)
            with self.assertRaises(ValueError):
                winusb.validate_output_paths(link, None)
            real = os.path.join(d, 'real')
            os.mkdir(real)
            os.symlink(real, os.path.join(d, 'linkdir'))
            with self.assertRaises(ValueError):
                winusb.validate_output_paths(os.path.join(d, 'linkdir', 'result.json'), None)

    def test_cli_rejects_before_opening_anything(self):
        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, 'passwd')
            with open(bad, 'w') as f:
                f.write('keep')
            for flag in ('--result', '--log'):
                rc = winusb.run_cli(['--winusb-bind', flag, bad], api_factory=lambda l: self.fail('api used'))
                self.assertEqual(rc, winusb.EXIT_USAGE)
            with open(bad) as f:
                self.assertEqual(f.read(), 'keep')

    def test_result_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'result.json')
            winusb.write_result(p, True, 'first')
            winusb.write_result(p, False, 'second')
            self.assertEqual(winusb.read_result(p)['message'], 'first')

    def test_app_does_not_load_heavy_modules_for_the_elevated_flags(self):
        app = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app.py')
        code = ('import runpy, sys\n'
                'sys.argv = ["app.py"] + sys.argv[1:]\n'
                'try:\n    runpy.run_path(%r, run_name="__main__")\n'
                'except SystemExit as e:\n    rc = e.code\n'
                'print(rc, [m for m in ("cv2", "PySide6", "usb", "numpy", "libusb_package") if m in sys.modules])\n' % app)
        with tempfile.TemporaryDirectory() as d:
            for argv in (['--winusb-bind', '--result', os.path.join(d, 'x.json')],
                         ['--winusb-bind', '--result', os.path.join(d, 'result.json'), '--log', os.path.join(d, 'winusb_setup.log')]):
                out = subprocess.run([sys.executable, '-c', code] + argv, capture_output=True, text=True,
                                     cwd=os.path.dirname(app), env=dict(os.environ, PYTHONPATH=os.path.dirname(app)))
                self.assertIn('[]', out.stdout, out.stdout + out.stderr)
                self.assertRegex(out.stdout, r'^[12] ')


class FakeApi:
    def __init__(self, service='usbccgp', after='winusb', guids=None, present=True, fail=False, reboot=False):
        self.fail, self.reboot = fail, reboot
        self.calls, self.service, self.after, self.present = [], service, after, present
        self.guids, self.installed = guids, False

    def node(self, svc):
        return winusb.CameraNode(PARENT + '\\0000000000012345', svc, '{x}', 0) if self.present else None

    def find_camera(self, verbose=False):
        self.calls.append('find')
        return self.node(self.after if self.installed else self.service)

    def write_guids(self, inst, guids=None):
        self.calls.append('write_guids')
        self.guids = list(guids or [winusb.APP_INTERFACE_GUID])

    def read_guids(self, inst):
        self.calls.append('read_guids')
        return self.guids

    def install_winusb(self, inst):
        self.calls.append('install')
        if self.fail:
            raise winusb.WinUsbError('DiInstallDevice failed')
        self.installed = True
        return self.reboot


class Orchestration(unittest.TestCase):
    def setUp(self):
        self.log = winusb.Log(os.devnull)

    def bind(self, api, **kw):
        return winusb.bind(api, self.log, sleep=lambda s: None, **kw)

    def test_happy_path_order(self):
        api = FakeApi()
        self.assertIn('now uses', self.bind(api))
        self.assertEqual([c for c in api.calls if c != 'find'], ['install', 'read_guids', 'write_guids'])
        self.assertLess(api.calls.index('install'), api.calls.index('write_guids'))

    def test_failed_install_never_writes_the_guid(self):
        for service in ('usbccgp', 'libusbK'):
            api = FakeApi(service=service, fail=True, guids=['{vendor}'])
            with self.assertRaises(winusb.WinUsbError):
                self.bind(api, replace_libusbk=True)
            self.assertNotIn('write_guids', api.calls)
            self.assertEqual(api.guids, ['{vendor}'])

    def test_existing_guids_are_kept(self):
        api = FakeApi(guids=['{vendor}'])
        self.bind(api)
        self.assertEqual(api.guids, ['{vendor}', winusb.APP_INTERFACE_GUID])

    def test_reboot_needed_is_not_a_failure(self):
        api = FakeApi(after='usbccgp', reboot=True)
        self.assertIn('restart', self.bind(api))
        self.assertNotIn('write_guids', api.calls)

    def test_waits_up_to_30_seconds(self):
        slept = []
        with self.assertRaises(winusb.WinUsbError):
            winusb.bind(FakeApi(after='usbccgp'), self.log, sleep=slept.append)
        self.assertGreaterEqual(sum(slept), 29)

    def test_already_bound_with_guid_does_nothing(self):
        api = FakeApi(service='WinUSB', guids=['{a}', winusb.APP_INTERFACE_GUID.upper()])
        self.assertIn('already uses', self.bind(api))
        self.assertNotIn('install', api.calls)
        self.assertNotIn('write_guids', api.calls)

    def test_already_bound_without_guid_writes_it_and_says_so(self):
        api = FakeApi(service='WinUSB', guids=['{other}'])
        msg = self.bind(api)
        self.assertNotIn('already uses', msg)
        self.assertIn('missing', msg)
        self.assertNotIn('install', api.calls)
        self.assertEqual(api.guids, ['{other}', winusb.APP_INTERFACE_GUID])

    def test_no_camera(self):
        with self.assertRaises(winusb.WinUsbError) as c:
            self.bind(FakeApi(present=False))
        self.assertIn('not connected', str(c.exception))
        self.assertEqual(c.exception.call, winusb.CALL_ABSENT)

    def test_libusbk_needs_confirmation(self):
        api = FakeApi(service='libusbK')
        with self.assertRaises(winusb.NeedsConfirmation):
            self.bind(api)
        self.assertNotIn('install', api.calls)
        self.bind(api, replace_libusbk=True)
        self.assertIn('install', api.calls)

    def test_32_bit_on_64_bit_is_refused(self):
        api = FakeApi()
        with mock.patch.object(winusb, 'is_wow64', return_value=True):
            with self.assertRaises(winusb.WinUsbError) as c:
                self.bind(api)
        self.assertIn('32-bit', str(c.exception))
        self.assertEqual(api.calls, [])

    def test_guid_rewritten_if_lost(self):
        api = FakeApi(guids=None)
        api.write_guids = lambda inst, guids=None: api.calls.append('write_guids')
        self.bind(api)
        self.assertEqual(api.calls.count('write_guids'), 1)

    def test_still_not_winusb_after_install_fails(self):
        with self.assertRaises(winusb.WinUsbError) as c:
            self.bind(FakeApi(after='usbccgp'))
        self.assertEqual(c.exception.call, 'verify')

    def test_revert(self):
        ran = []
        api = FakeApi(service='WinUSB')
        msg = winusb.revert(api, self.log, run=lambda a: ran.append(a) or (0, ''))
        self.assertEqual(ran[0][0], '/remove-device')
        self.assertIn('0000000000012345', ran[0][1])
        self.assertEqual(ran[1], ['/scan-devices'])
        self.assertIn('replug', msg)

    def test_revert_remove_ok_scan_failed(self):
        with self.assertRaises(winusb.WinUsbError) as c:
            winusb.revert(FakeApi(service='WinUSB'), self.log, run=lambda a: (0, '') if a[0] == '/remove-device' else (1, ''))
        self.assertIn('replug', str(c.exception))

    def test_revert_needs_new_enough_windows(self):
        with mock.patch.object(winusb, 'remove_device_supported', return_value=False):
            with self.assertRaises(winusb.WinUsbError) as c:
                winusb.revert(FakeApi(service='WinUSB'), self.log, run=lambda a: (0, ''))
        self.assertIn('1607', str(c.exception))

    def test_pnputil_fails_closed(self):
        with mock.patch.object(winusb, 'system_directory', return_value='/nonexistent'):
            with self.assertRaises(winusb.WinUsbError):
                winusb.pnputil_exe()
        with mock.patch.object(winusb, 'system_directory', side_effect=AttributeError('no windll')):
            with self.assertRaises(winusb.WinUsbError):
                winusb.pnputil_exe()

    def test_revert_refuses_other_drivers_and_pnputil_errors(self):
        with self.assertRaises(winusb.WinUsbError):
            winusb.revert(FakeApi(service='libusbK'), self.log, run=lambda a: (0, ''))
        with self.assertRaises(winusb.WinUsbError) as c:
            winusb.revert(FakeApi(service='WinUSB'), self.log, run=lambda a: (5, 'denied'))
        self.assertIn('0x5', str(c.exception))

    @staticmethod
    def read(path, as_json=True):
        with open(path, encoding='utf-8') as f:
            return json.load(f) if as_json else f.read()

    def test_cli_writes_result_and_exit_code(self):
        with tempfile.TemporaryDirectory() as d:
            res, log = os.path.join(d, 'result.json'), os.path.join(d, 'winusb_setup.log')
            argv = ['--winusb-bind', '--result', res, '--log', log]
            with mock.patch.object(winusb, 'bind', side_effect=winusb.fail(winusb.Log(log), 'DiInstallDevice', 5)):
                rc = winusb.run_cli(argv, api_factory=lambda l: FakeApi())
            self.assertEqual(rc, winusb.EXIT_ERROR)
            r = self.read(res)
            self.assertFalse(r['ok'])
            self.assertEqual(r['call'], 'DiInstallDevice')
            self.assertIn('DiInstallDevice FAILED', self.read(log, False))
            os.remove(res)
            rc = winusb.run_cli(argv, api_factory=lambda l: FakeApi(service='WinUSB', guids=[winusb.APP_INTERFACE_GUID]))
            self.assertEqual(rc, winusb.EXIT_OK)
            self.assertTrue(self.read(res)['ok'])
            os.remove(res)
            with mock.patch.object(winusb, 'bind', side_effect=KeyError('x')):
                self.assertEqual(winusb.run_cli(argv, api_factory=lambda l: FakeApi()), winusb.EXIT_ERROR)
            self.assertIsNone(winusb.run_cli(['--something']))
            self.assertEqual(winusb.run_cli(['--winusb-bind', '--log']), winusb.EXIT_USAGE)


class DriverIntegration(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(driver.winusb, 'system_directory', return_value='C:\\Windows\\System32')
        p.start()
        self.addCleanup(p.stop)

    def elevated(self, ok=True, message='done', code=0):
        """Stand-in for run_elevated that behaves like the elevated child: writes the result file."""
        seen = {}

        def fake(exe, params='', cwd=None, show=0, timeout=900):
            seen.update(exe=exe, params=params)
            args = winusb.parse_args(self._split(params))
            winusb.write_result(args['result'], ok, message, 'X', 0, args['log'])
            return code
        return fake, seen

    @staticmethod
    def _split(params):
        out, cur, q = [], '', False
        for ch in params:
            if ch == '"':
                q = not q
            elif ch == ' ' and not q:
                if cur:
                    out.append(cur)
                cur = ''
            else:
                cur += ch
        return out + ([cur] if cur else [])

    def test_install_runs_elevated_with_the_flag(self):
        fake, seen = self.elevated()
        with mock.patch.object(driver, 'is_windows', return_value=True), \
                mock.patch.object(driver, 'binding_state', return_value='usbccgp'), \
                mock.patch.object(driver, 'run_elevated', fake):
            msg = driver.install_inbox_winusb()
        self.assertIn('--winusb-bind', seen['params'])
        self.assertIn('app.py', seen['params'])
        self.assertIn('replug', msg)
        self.assertNotIn('--replace-libusbk', seen['params'])

    def test_frozen_launches_the_exe_itself(self):
        fake, seen = self.elevated()
        with mock.patch.object(driver, 'is_windows', return_value=True), mock.patch.object(sys, 'frozen', True, create=True), \
                mock.patch.object(driver, 'binding_state', return_value='libusbk'), mock.patch.object(driver, 'run_elevated', fake):
            driver.install_inbox_winusb(replace_libusbk=True)
        self.assertEqual(seen['exe'], sys.executable)
        self.assertNotIn('app.py', seen['params'])
        self.assertIn('--replace-libusbk', seen['params'])

    def test_elevates_from_system32(self):
        seen = {}
        with mock.patch.object(driver, 'is_windows', return_value=True), \
                mock.patch.object(driver, 'binding_state', return_value='usbccgp'), \
                mock.patch.object(driver, 'run_elevated', side_effect=lambda *a, **k: seen.update(k) or 1):
            with self.assertRaises(driver.DriverError):
                driver.install_inbox_winusb()
        self.assertEqual(seen['cwd'], 'C:\\Windows\\System32')

    def test_winusb_without_guid_is_still_elevated(self):
        fake, seen = self.elevated(message='The camera already used the Windows driver (WinUSB), but its interface registration was missing and has been added.')
        with mock.patch.object(driver, 'is_windows', return_value=True), \
                mock.patch.object(driver, 'binding_state', return_value='winusb'), \
                mock.patch.object(driver.winusb, 'app_guid_present', return_value=False), \
                mock.patch.object(driver, 'run_elevated', fake):
            msg = driver.install_inbox_winusb()
        self.assertIn('--winusb-bind', seen['params'])
        self.assertIn('replug', msg)

    def test_absent_result_maps_to_camera_absent(self):
        fake, _ = self.elevated(ok=False, message='not connected')
        def absent(exe, params='', **k):
            winusb.write_result(winusb.parse_args(self._split(params))['result'], False, 'Plug it in', winusb.CALL_ABSENT)
            return 1
        with mock.patch.object(driver, 'is_windows', return_value=True), \
                mock.patch.object(driver, 'binding_state', return_value='usbccgp'), mock.patch.object(driver, 'run_elevated', absent):
            with self.assertRaises(driver.DriverCameraAbsent):
                driver.install_inbox_winusb()

    def test_preconditions(self):
        with mock.patch.object(driver, 'is_windows', return_value=True), mock.patch.object(driver, 'run_elevated') as run:
            with mock.patch.object(driver, 'binding_state', return_value='winusb'), \
                    mock.patch.object(driver.winusb, 'app_guid_present', return_value=True):
                self.assertIn('already', driver.install_inbox_winusb())
            with mock.patch.object(driver, 'binding_state', return_value='absent'), self.assertRaises(driver.DriverError):
                driver.install_inbox_winusb()
            with mock.patch.object(driver, 'binding_state', return_value='libusbk'), \
                    self.assertRaises(driver.DriverNeedsConfirmation):
                driver.install_inbox_winusb()
        run.assert_not_called()

    def test_uac_declined(self):
        with mock.patch.object(driver, 'is_windows', return_value=True), \
                mock.patch.object(driver, 'binding_state', return_value='usbccgp'), \
                mock.patch.object(driver, 'run_elevated', side_effect=driver.DriverCancelled('denied')):
            with self.assertRaises(driver.DriverCancelled):
                driver.install_inbox_winusb()

    def test_failure_message_comes_through(self):
        fake, _ = self.elevated(ok=False, message='DiInstallDevice failed: access denied (0x5). Details: x.log', code=1)
        with mock.patch.object(driver, 'is_windows', return_value=True), \
                mock.patch.object(driver, 'binding_state', return_value='usbccgp'), mock.patch.object(driver, 'run_elevated', fake):
            with self.assertRaises(driver.DriverError) as c:
                driver.install_inbox_winusb()
        self.assertIn('DiInstallDevice', str(c.exception))

    def test_no_result_file(self):
        with mock.patch.object(driver, 'is_windows', return_value=True), \
                mock.patch.object(driver, 'binding_state', return_value='usbccgp'), \
                mock.patch.object(driver, 'run_elevated', return_value=3):
            with self.assertRaises(driver.DriverError) as c:
                driver.install_inbox_winusb()
        self.assertIn('winusb_setup.log', str(c.exception))

    def test_remove(self):
        fake, seen = self.elevated(message='removed')
        with mock.patch.object(driver, 'is_windows', return_value=True), mock.patch.object(driver, 'run_elevated', fake):
            self.assertEqual(driver.remove_inbox_winusb(), 'removed')
        self.assertIn('--winusb-revert', seen['params'])

    def test_not_windows(self):
        with self.assertRaises(driver.DriverError):
            driver._elevated_winusb('bind')


if __name__ == '__main__':
    unittest.main()


@unittest.skipUnless(sys.platform == 'win32', 'runs the real SetupAPI layer: Windows only')
class RealWindows(unittest.TestCase):
    """The real ctypes layer on a machine without the camera (the CI runner). Read-only: nothing is bound, reverted,
    installed or written, and nothing needs admin rights."""
    ENTRY_POINTS = {
        'sp': ('SetupDiGetClassDevsW', 'SetupDiDestroyDeviceInfoList', 'SetupDiEnumDeviceInfo',
               'SetupDiGetDeviceRegistryPropertyW', 'SetupDiGetDeviceInstanceIdW', 'SetupDiCreateDeviceInfoList',
               'SetupDiOpenDeviceInfoW', 'SetupDiGetDeviceInstallParamsW', 'SetupDiSetDeviceInstallParamsW',
               'SetupDiBuildDriverInfoList', 'SetupDiEnumDriverInfoW', 'SetupDiGetDriverInfoDetailW',
               'SetupDiSetSelectedDriverW', 'SetupDiCallClassInstaller', 'SetupDiOpenDevRegKey',
               'SetupDiCreateDevRegKeyW'),
        'nd': ('DiInstallDevice',),
        'adv': ('RegSetValueExW', 'RegQueryValueExW', 'RegCloseKey'),
    }

    def test_every_entry_point_resolves_with_its_types(self):
        api = winusb.load_api(winusb.Log(os.devnull))
        for lib, names in self.ENTRY_POINTS.items():
            for name in names:
                fn = getattr(getattr(api, lib), name)
                self.assertIsNotNone(fn.argtypes, name)
                self.assertGreater(len(fn.argtypes), 0, name)
                self.assertIsNotNone(fn.restype, name)

    def test_the_declared_functions_are_the_ones_checked(self):
        declared = set(re.findall(r'd\((?:sp|self\.nd|adv)\.(\w+),', inspect.getsource(winusb.SetupApi._declare)))
        self.assertEqual(declared, {n for names in self.ENTRY_POINTS.values() for n in names})

    def test_no_camera_means_absent(self):
        self.assertEqual(winusb.binding_state(), 'absent')
        self.assertIsNone(winusb.app_guid_present())

    def test_find_camera_finds_nothing_and_does_not_raise(self):
        api = winusb.load_api(winusb.Log(os.devnull))
        self.assertIsNone(api.find_camera())
        self.assertIsNone(api.find_camera(verbose=True))

    def test_diagnose_report(self):
        text = winusb.diagnose()
        for field in ('python ', '64-bit', 'platform win32', 'wow64', 'log file:', 'libusb:', 'camera: not connected'):
            self.assertIn(field, text)
        self.assertNotIn('camera query failed', text)
        self.assertIn('usbflags', text)

    @unittest.skipUnless(X64, 'struct sizes are the 64-bit ones')
    def test_struct_sizes_on_the_real_platform(self):
        self.assertEqual(ctypes.sizeof(winusb.GUID), 16)
        self.assertEqual(ctypes.sizeof(winusb.SP_DEVINFO_DATA), 32)
        self.assertEqual(ctypes.sizeof(winusb.SP_DEVINSTALL_PARAMS_W), 584)
        self.assertEqual(ctypes.sizeof(winusb.SP_DRVINFO_DATA_W), 1568)
        self.assertEqual(winusb.DETAIL_SIZEOF, 1584)
