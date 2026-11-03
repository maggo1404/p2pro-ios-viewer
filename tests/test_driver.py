"""Run: python -m pytest tests  (or python -m unittest discover tests). No network, no Windows needed.
Set P2PRO_SETUP_EXE to a downloaded Thermal Master installer to also run the real-extraction test."""
import os
import sys
import tempfile
import unittest
import zipfile

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
        html = open(os.path.join(HERE, 'fixtures', 'download_center_snippet.html'), encoding='utf-8').read()
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
