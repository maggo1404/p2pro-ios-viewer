"""Run: python -m pytest tests  (or python -m unittest discover tests). No network, no Windows needed.
Set P2PRO_SETUP_EXE to a downloaded Thermal Master installer to also run the real-extraction test."""
import os
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import driver  # noqa: E402
import inno  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

PNPUTIL_EN = """Microsoft PnP Utility

Published Name:     oem42.inf
Original Name:      P2Pro.inf
Provider Name:      libusbK
Class Name:         libusbK Usb Devices
Driver Version:     04/11/2021 3.1.0.0

Published Name:     oem3.inf
Original Name:      other.inf
"""
PNPUTIL_DE = """Microsoft PnP-Dienstprogramm

Veröffentlichter Name: oem42.inf
Originalname:          p2pro.INF
Anbietername:          libusbK
"""


class FindUrl(unittest.TestCase):
    def test_fixture(self):
        with open(os.path.join(HERE, 'fixtures', 'download_center_snippet.html'), encoding='utf-8') as f:
            html = f.read()
        self.assertEqual(driver.find_download_url(html), driver.FALLBACK_URL)

    def test_relative_and_other_markup(self):
        html = '<a class="x" href=\'/files/a%20b.exe\'><span>Temp  Master\n (P-Serie)</span></a><a href="/no">Other</a>'
        self.assertEqual(driver.find_download_url(html), 'https://thermalmaster.com/files/a%20b.exe')

    def test_missing(self):
        with self.assertRaises(driver.DriverError):
            driver.find_download_url('<a href="https://x/y.exe">P-Serie Android 10 APP</a>')

    def test_rejects_http(self):
        with self.assertRaises(driver.DriverError):
            driver.find_download_url('<a href="http://x/y.exe">Temp Master (P-Serie)</a>')


class Pnputil(unittest.TestCase):
    def test_english(self):
        self.assertTrue(driver.driver_store_has(PNPUTIL_EN))

    def test_german(self):
        self.assertTrue(driver.driver_store_has(PNPUTIL_DE))

    def test_absent(self):
        self.assertFalse(driver.driver_store_has(PNPUTIL_EN.replace('P2Pro', 'P3')))
        self.assertFalse(driver.driver_store_has(''))

    def test_services_and_codes(self):
        self.assertTrue(driver.service_is_usable('libusbK'))
        self.assertTrue(driver.service_is_usable('WinUSB'))
        self.assertFalse(driver.service_is_usable('usbccgp'))
        self.assertTrue(driver.pnputil_ok(3010) and driver.pnputil_ok(259) and not driver.pnputil_ok(1))
        self.assertIn('not signed', driver.pnputil_message(0xE000022F))


class Installed(unittest.TestCase):
    def check(self, state, store=None, reg=None):
        with mock.patch.object(driver, 'is_windows', return_value=True), \
                mock.patch.object(driver.winusb, 'binding_state', return_value=state), \
                mock.patch.object(driver, '_store_check', return_value=store), \
                mock.patch.object(driver, '_registry_check', return_value=reg):
            return driver.is_driver_installed()

    def test_bound_camera_counts_as_installed(self):
        self.assertTrue(self.check('winusb', store=False, reg=False))
        self.assertTrue(self.check('libusbk', store=False, reg=False))

    def test_old_behaviour_is_kept(self):
        self.assertTrue(self.check('usbccgp', store=True))                  # P2Pro.inf in the store is still enough
        self.assertTrue(self.check('absent', reg=True))
        self.assertFalse(self.check('usbccgp', store=False, reg=False))
        self.assertIsNone(self.check('absent', store=None, reg=None))

    def test_not_windows(self):
        with mock.patch.object(driver, 'is_windows', return_value=False):
            self.assertTrue(driver.is_driver_installed())


class Traversal(unittest.TestCase):
    def test_safe_join(self):
        with tempfile.TemporaryDirectory() as d:
            for bad in ('../x', 'a/../../x', '/etc/passwd', 'C:\\x', 'a\\..\\..\\x'):
                with self.assertRaises(driver.DriverError, msg=bad):
                    driver.safe_join(d, bad)
            self.assertTrue(driver.safe_join(d, 'drivers\\inf\\a.inf').startswith(os.path.abspath(d)))

    def test_zip_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            z = os.path.join(d, 'evil.zip')
            with zipfile.ZipFile(z, 'w') as zf:
                zf.writestr('drivers/P2Pro.inf', '')
                zf.writestr('../evil.txt', 'x')
            with self.assertRaises(driver.DriverError):
                driver.extract_driver(z, d)
            self.assertFalse(os.path.exists(os.path.join(os.path.dirname(d), 'evil.txt')))

    def test_zip_ok_and_staging(self):
        inf = ('[SourceDisksFiles.amd64]\r\nlibusbK.sys = 1,libusbK/amd64\r\n').encode('utf-16')
        with tempfile.TemporaryDirectory() as d:
            z = os.path.join(d, 'ok.zip')
            with zipfile.ZipFile(z, 'w') as zf:
                zf.writestr('Treiber/inf/P2Pro.inf', inf)
                zf.writestr('Treiber/libusbK/amd64/libusbK.sys', b'MZ')
                zf.writestr('app/other.txt', 'ignored')
            wd = os.path.join(d, 'w')
            os.mkdir(wd)
            p = driver.extract_driver(z, wd)
            self.assertEqual(os.path.basename(os.path.dirname(p)), 'Treiber')
            self.assertFalse(os.path.exists(os.path.join(wd, 'unpacked', 'app')))

    def test_inno_relpath(self):
        self.assertEqual(inno.safe_relpath('{app}\\drivers\\inf\\P2Pro.inf'), ['drivers', 'inf', 'P2Pro.inf'])
        for bad in ('{app}\\..\\x', 'C:\\x', '{app}\\a\\..\\..\\b', '\\\\srv\\share\\x'):
            with self.assertRaises(inno.InnoError, msg=bad):
                inno.safe_relpath(bad)

    def test_not_an_archive(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, 'x')
            with open(f, 'wb') as fh:
                fh.write(b'MZ' + b'\0' * 100)
            with self.assertRaises(driver.DriverError):
                driver.extract_driver(f, d)


class VendorFallback(unittest.TestCase):
    """install_from_download with pnputil failing: the vendor installer only runs after an explicit yes."""

    def run_pipeline(self, **kw):
        patches = dict(fetch_download_url='http://x', download='a', extract_driver='/x/P2Pro.inf',
                       install_driver=0xE000022F, install_vendor_fallback=True, is_driver_installed=True)
        patches.update(kw.pop('patches', {}))
        started = {}
        with tempfile.TemporaryDirectory() as td, mock.patch.object(driver, 'workdir') as wd:
            wd.return_value.__enter__.return_value = td
            ms = {}
            for name, val in patches.items():
                m = mock.patch.object(driver, name, **(dict(side_effect=val) if isinstance(val, BaseException) else dict(return_value=val)))
                ms[name] = m.start()
                self.addCleanup(m.stop)
            try:
                return driver.install_from_download(**kw), ms
            finally:
                started['ms'] = ms
                self.ms = ms

    def test_no_callback_skips_the_fallback(self):
        with self.assertRaises(driver.DriverError) as cm:
            self.run_pipeline()
        self.ms['install_vendor_fallback'].assert_not_called()
        self.assertIn('not signed', str(cm.exception))
        self.assertIn('nothing was installed', str(cm.exception))
        self.assertIn('no certificate was added', str(cm.exception))
        self.assertNotIn('PC app', str(cm.exception))

    def test_declined_skips_the_fallback(self):
        ask = mock.Mock(return_value=False)
        with self.assertRaises(driver.DriverError):
            self.run_pipeline(confirm_fallback=ask)
        ask.assert_called_once()
        self.ms['install_vendor_fallback'].assert_not_called()

    def test_a_failing_callback_counts_as_no(self):
        with self.assertRaises(driver.DriverError):
            self.run_pipeline(confirm_fallback=mock.Mock(side_effect=RuntimeError('x')))
        self.ms['install_vendor_fallback'].assert_not_called()

    def test_confirmed_runs_the_fallback(self):
        msg, ms = self.run_pipeline(confirm_fallback=lambda: True)
        ms['install_vendor_fallback'].assert_called_once()
        self.assertIn('vendor installer', msg)

    def test_confirmed_but_driver_not_detected_discloses_the_certificate(self):
        with mock.patch.object(driver.time, 'sleep'), self.assertRaises(driver.DriverError) as cm:
            self.run_pipeline(confirm_fallback=lambda: True, patches=dict(is_driver_installed=False))
        self.assertIn('libwdi', str(cm.exception))
        self.assertIn('certlm.msc', str(cm.exception))

    def test_uac_cancel_propagates(self):
        with self.assertRaises(driver.DriverCancelled):
            self.run_pipeline(confirm_fallback=lambda: True,
                              patches=dict(install_vendor_fallback=driver.DriverCancelled('denied')))

    def test_pnputil_success_never_asks(self):
        ask = mock.Mock()
        self.run_pipeline(confirm_fallback=ask, patches=dict(install_driver=0))
        ask.assert_not_called()
        self.ms['install_vendor_fallback'].assert_not_called()

    def test_docstring_mentions_the_certificate(self):
        self.assertIn('libwdi', driver.install_vendor_fallback.__doc__)


@unittest.skipUnless(os.environ.get('P2PRO_SETUP_EXE'), 'set P2PRO_SETUP_EXE to the downloaded installer')
class RealInstaller(unittest.TestCase):
    def test_extract(self):
        with driver.workdir() as wd:
            inf = driver.extract_driver(os.environ['P2PRO_SETUP_EXE'], wd)
            self.assertEqual(os.path.basename(inf), 'P2Pro.inf')
            d = os.path.dirname(inf)
            self.assertTrue(os.path.isfile(os.path.join(d, 'libusbK', 'amd64', 'libusbK.sys')))
            self.assertIn('VID_0BDA&PID_5840', driver.read_inf_text(inf))


if __name__ == '__main__':
    unittest.main()
