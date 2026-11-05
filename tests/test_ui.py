"""GUI tests: the real MainWindow on Qt's offscreen platform, without a camera."""
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import numpy as np
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton, QToolBar

import app as A
import ui

QT = QApplication.instance() or QApplication([])


def scheme(window, text):
    p = QPalette()
    for role, c in ((QPalette.Window, window), (QPalette.WindowText, text), (QPalette.ButtonText, text)):
        p.setColor(role, QColor(c))
    return p


LIGHT, DARK = scheme('#F3F3F3', '#000000'), scheme('#202020', '#FFFFFF')


class Tokens(unittest.TestCase):
    def test_text_colours_reach_4_5_to_1_in_both_appearances(self):
        for pal in (LIGHT, DARK):
            for fg in (ui.secondary(pal), ui.signal(pal)):
                self.assertGreaterEqual(ui.contrast(fg, pal.color(QPalette.Window)), 4.5)

    def test_contrast_reference_values(self):
        self.assertAlmostEqual(ui.contrast(QColor('#000000'), QColor('#FFFFFF')), 21, places=3)

    def test_every_icon_draws(self):
        for name in ('connect', 'shutter', 'snapshot', 'record', 'stop', 'stream', 'inspector'):
            self.assertFalse(ui.icon(name, DARK).isNull())
        with self.assertRaises(ValueError):
            ui.icon('nope', DARK)


class Window(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.settings = os.path.join(self.dir.name, 'settings.json')
        for p in (mock.patch.object(A, 'SETTINGS_FILE', self.settings), mock.patch.object(A, 'APP_DIR', self.dir.name),
                  mock.patch.object(A.MainWindow, 'connect_camera'), mock.patch.object(A.MainWindow, 'check_driver')):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.dir.cleanup)
        # driver.binding_state talks to SetupAPI, so it must never run on the UI thread; on Windows refresh_binding
        # legitimately calls it from a worker. Record the calling threads instead of forbidding every call.
        self.binding_threads = []

        def fake_binding_state():
            self.binding_threads.append(threading.current_thread())
            return 'usbccgp'
        p = mock.patch.object(A.driver, 'binding_state', side_effect=fake_binding_state)
        p.start()
        self.addCleanup(p.stop)
        # the 4 s refresh after a driver job must not fire into a later test; record it instead
        p = mock.patch.object(A.QTimer, 'singleShot')
        self.single_shots = p.start()
        self.addCleanup(p.stop)
        self.threads_before = set(threading.enumerate())
        self.win = self.new_window()
        self.addCleanup(self.settle)                    # runs first: before the window is deleted and patches end

    def settle(self):
        """Wait for worker threads started since setUp (refresh_binding) and deliver their signals."""
        for t in set(threading.enumerate()) - self.threads_before:
            t.join(5)
        QT.processEvents()

    def assert_binding_never_on_ui_thread(self):
        self.assertNotIn(threading.main_thread(), self.binding_threads)

    def new_window(self):
        w = A.MainWindow()
        w.timer.stop()
        self.addCleanup(w.deleteLater)
        return w

    def live_frame(self):
        self.win.temp = np.linspace(20, 30, A.IMG_W * A.IMG_H, dtype=np.float32).reshape(A.IMG_H, A.IMG_W)
        self.win.view.set_image(self.win.compose(self.win.temp)[0])
        self.win.update_actions()

    def test_every_toolbar_action_is_in_the_menu_bar(self):
        in_menus = {a for m in self.win.menuBar().actions() for a in m.menu().actions()}
        for a in self.win.findChild(QToolBar).actions():
            if a.text():                                # skips separators and the spacer / status widgets
                self.assertIn(a, in_menus, a.text())

    def test_shortcuts_are_unique(self):
        keys = [a.shortcut().toString() for m in self.win.menuBar().actions() for a in m.menu().actions()
                if not a.shortcut().isEmpty()]
        self.assertEqual(len(keys), len(set(keys)), keys)
        for key in ('S', 'C', 'R', 'A', 'Del', 'Esc'):      # existing shortcuts survive the move into the menus
            self.assertIn(key, keys)

    def test_commands_without_a_frame_are_disabled(self):
        self.assertFalse(self.win.act_snapshot.isEnabled())
        self.assertFalse(self.win.act_shutter.isEnabled())
        self.live_frame()
        self.assertTrue(self.win.act_snapshot.isEnabled())
        self.assertTrue(self.win.btn_cal.isEnabled())

    def test_unit_segments_convert_the_range(self):
        self.win.sp_min.setValue(20)
        self.win.cb_fahrenheit.click()
        self.assertTrue(self.win.cb_fahrenheit.isChecked())
        self.assertAlmostEqual(self.win.sp_min.value(), 68, places=1)
        group = self.win.cb_fahrenheit.group()
        celsius = next(b for b in group.buttons() if b is not self.win.cb_fahrenheit)
        celsius.click()
        self.assertFalse(self.win.cb_fahrenheit.isChecked())
        self.assertAlmostEqual(self.win.sp_min.value(), 20, places=1)

    def test_range_fields_follow_the_scale_mode(self):
        self.assertFalse(self.win.sp_min.isEnabled())
        self.win.rb_fixed.click()
        self.assertTrue(self.win.sp_min.isEnabled())
        self.assertFalse(self.win.rb_auto.isChecked())

    def test_inspector_can_be_hidden(self):
        self.win.show()
        self.assertTrue(self.win.scroll.isVisible())
        self.win.act_inspector.trigger()
        self.assertFalse(self.win.scroll.isVisible())
        self.win.close()

    def test_settings_round_trip(self):
        w = self.win
        w.cb_fahrenheit.click()
        w.rb_fixed.click()
        w.sp_min.setValue(50)
        w.sections['output'].set_expanded(True)
        w.sections['display'].set_expanded(False)
        w.act_inspector.setChecked(False)
        w.enh['dde'][0].setChecked(True)
        w.close()
        with open(self.settings) as f:
            self.assertFalse(json.load(f)['inspector'])
        again = self.new_window()
        self.assertTrue(again.cb_fahrenheit.isChecked())
        self.assertAlmostEqual(again.sp_min.value(), 50, places=1)     # stored in °F, not converted twice
        self.assertTrue(again.rb_fixed.isChecked())
        self.assertTrue(again.sections['output'].expanded())
        self.assertFalse(again.sections['display'].expanded())
        self.assertFalse(again.act_inspector.isChecked())
        self.assertTrue(again.enh['dde'][1].isEnabled())
        self.assertFalse(again.enh['sharpen'][1].isEnabled())

    def test_old_settings_file_without_new_keys(self):
        with open(self.settings, 'w') as f:
            json.dump({'palette': 'Ironbow', 'fahrenheit': False, 'enh': {}}, f)
        w = self.new_window()
        self.assertTrue(w.sections['measurement'].expanded())
        self.assertFalse(w.sections['output'].expanded())

    def test_appearance_change_redraws_status_colour(self):
        self.win.set_connection('Not Connected')
        old = QT.palette()
        self.addCleanup(QT.setPalette, old)
        QT.setPalette(DARK)
        QT.processEvents()
        self.win.apply_theme()
        self.assertIn(ui.signal(DARK).name(), self.win.chip.text())
        QT.setPalette(LIGHT)
        QT.processEvents()
        self.assertIn(ui.signal(LIGHT).name(), self.win.chip.text())

    def test_recording_state_is_visible_in_the_toolbar(self):
        self.live_frame()
        rec = mock.MagicMock(path='/x/thermal.mp4', backend='ffmpeg', size=(840, 588))
        with mock.patch.object(A, 'Recorder', return_value=rec):
            self.win.act_rec.trigger()
        self.assertIn('REC', self.win.chip.text())
        self.assertEqual(self.win.act_rec.text(), 'Stop Recording')
        self.assertEqual(self.win.act_rec.iconText(), 'Stop')
        self.win.act_rec.trigger()
        rec.close.assert_called_once()
        self.assertNotIn('REC', self.win.chip.text())
        self.assertEqual(self.win.act_rec.iconText(), 'Record')

    def test_stream_url_only_while_streaming(self):
        self.win.show()
        self.assertFalse(self.win.rtsp_url.isVisible())
        srv = mock.MagicMock(url='rtsp://1.2.3.4:8554/thermal', size=(840, 588))
        with mock.patch.object(A, 'RtspServer', return_value=srv):
            self.win.toggle_rtsp()
        self.assertTrue(self.win.rtsp_url.isVisible())
        self.assertEqual(self.win.rtsp_url.text(), srv.url)
        self.assertIn('RTSP', self.win.chip.text())
        self.win.toggle_rtsp()
        self.assertFalse(self.win.rtsp_url.isVisible())
        self.win.close()

    def test_snapshot_still_writes_files(self):
        self.live_frame()
        self.win.act_snapshot.trigger()
        names = os.listdir(os.path.join(self.dir.name, 'snapshots'))
        self.assertEqual(sorted(os.path.splitext(n)[1] for n in names), ['.csv', '.png'])

    def test_missing_driver_shows_a_notice_not_an_alert(self):
        self.win.show()
        self.win.drv_connect_after = True
        with mock.patch.object(QMessageBox, 'exec') as dialog:
            self.win.on_log('@driver missing')
        dialog.assert_not_called()
        self.assertTrue(self.win.banner.isVisible())
        self.win.connect_camera.assert_called()         # still tries: the driver check can be wrong
        self.win.close()

    def test_declining_the_install_changes_nothing(self):
        self.win.show()
        self.win.on_log('@driver missing')
        with mock.patch.object(QMessageBox, 'exec'), mock.patch.object(A.driver, 'install_from_download') as install:
            self.win.offer_driver_install()
        install.assert_not_called()
        self.assertFalse(self.win.drv_busy)
        self.assertTrue(self.win.banner.isVisible())
        self.win.close()

    def run_install(self, choose, **patches):
        """Open the driver dialog, press the button `choose(box)` and wait for the worker."""
        self.win.show()
        self.win.act_driver.setVisible(True)            # as on Windows: Qt reports hidden actions as disabled
        self.win.on_log('@driver missing')
        with mock.patch.object(QMessageBox, 'exec', lambda box: choose(box).click()), \
                mock.patch.object(QMessageBox, 'information') as info, mock.patch.object(QMessageBox, 'warning') as warn:
            self.win.binding = patches.pop('state', 'usbccgp')
            for name, target in patches.items():
                p = mock.patch.object(A.driver, name, **target)
                self.calls = getattr(self, 'calls', {})
                self.calls[name] = p.start()
                self.addCleanup(p.stop)
            self.win.offer_driver_install()
            deadline = time.time() + 5
            while self.win.drv_busy and time.time() < deadline:
                QT.processEvents()
                time.sleep(0.01)
        self.settle()
        self.assert_binding_never_on_ui_thread()        # the dialog uses the cached state
        return info, warn

    def test_refresh_binding_on_windows_reads_the_state_off_the_ui_thread(self):
        with mock.patch.object(A.driver, 'is_windows', return_value=True):
            self.win.refresh_binding()
        self.settle()
        self.assertEqual(len(self.binding_threads), 1)
        self.assertNotEqual(self.binding_threads[0], threading.main_thread())
        self.assertEqual(self.win.binding, 'usbccgp')

    def test_refresh_binding_elsewhere_does_nothing(self):
        with mock.patch.object(A.driver, 'is_windows', return_value=False):
            self.win.refresh_binding()
        self.settle()
        self.assertEqual(self.binding_threads, [])

    def test_the_dialog_offers_the_windows_driver_first(self):
        seen = {}

        def look(box):
            seen['default'] = box.defaultButton().text()
            seen['buttons'] = [b.text() for b in box.buttons()]
            seen['text'] = box.informativeText()
            return box.button(QMessageBox.Cancel)
        self.run_install(look, state='libusbk')
        self.assertEqual(seen['default'], 'Use Windows Driver')
        self.assertIn('Download Vendor Driver…', seen['buttons'])
        self.assertIn('libusbK', seen['text'])
        self.assertIn('Thermal Master app may need', seen['text'])
        self.assertIn('(recommended)', seen['text'])
        self.assertNotIn('not been confirmed', seen['text'])
        self.assertNotIn('tested option', seen['text'])
        self.assertFalse(self.win.drv_busy)

    def test_windows_driver_flow_clears_the_notice(self):
        info, warn = self.run_install(lambda box: box.defaultButton(),
                                      install_inbox_winusb=dict(return_value='installed'),
                                      install_from_download=dict(return_value='x'))
        self.calls['install_inbox_winusb'].assert_called_once()
        self.calls['install_from_download'].assert_not_called()
        info.assert_called_once()
        warn.assert_not_called()
        self.assertFalse(self.win.drv_busy)
        self.assertFalse(self.win.banner.isVisible())
        self.assertTrue(self.win.act_driver.isEnabled())
        self.win.close()

    def test_replacing_libusbk_is_passed_on(self):
        self.run_install(lambda box: box.defaultButton(), state='libusbk',
                         install_inbox_winusb=dict(return_value='installed'))
        self.assertEqual(self.calls['install_inbox_winusb'].call_args.args[1], True)
        self.win.close()

    def test_vendor_flow_still_available(self):
        info, _ = self.run_install(lambda box: [b for b in box.buttons() if b.text().startswith('Download')][0],
                                   install_inbox_winusb=dict(return_value='x'),
                                   install_from_download=dict(return_value='installed'))
        self.calls['install_from_download'].assert_called_once()
        self.calls['install_inbox_winusb'].assert_not_called()
        info.assert_called_once()
        self.assertFalse(self.win.banner.isVisible())
        self.win.close()

    def test_fallback_warning_defaults_to_cancel_and_names_the_certificate(self):
        box, run = self.win.make_fallback_box()
        self.assertEqual(box.defaultButton(), box.button(QMessageBox.Cancel))
        self.assertEqual(box.escapeButton(), box.button(QMessageBox.Cancel))
        self.assertEqual(run.text(), 'Run Vendor Installer')
        text = box.informativeText()
        for needle in ('unsigned', 'libwdi', 'Trusted Root', 'Trusted Publishers', 'certlm.msc', 'other camera models',
                       'Use Windows Driver'):
            self.assertIn(needle, text)
        self.win.close()

    def vendor_fallback_run(self, accept):
        """Vendor flow with pnputil failing; the warning is answered on the UI thread."""
        def fake_install(status, progress, cancel, confirm_fallback=None):
            if confirm_fallback is not None and confirm_fallback():
                return 'ran'
            raise A.driver.DriverError('declined')
        seen = {}

        def exec_(box):
            seen['dialog_hidden'] = not self.win.drv_dialog.isVisible()
            seen['thread'] = threading.current_thread() is threading.main_thread()
            run = [b for b in box.buttons() if b.text() == 'Run Vendor Installer'][0]
            (run if accept else box.button(QMessageBox.Cancel)).click()
        self.win.show()
        with mock.patch.object(QMessageBox, 'exec', exec_), mock.patch.object(QMessageBox, 'information'), \
                mock.patch.object(QMessageBox, 'warning') as warn, \
                mock.patch.object(A.driver, 'install_from_download', fake_install):
            self.win.start_driver_job('vendor', 'Installing Camera Driver')
            deadline = time.time() + 5
            while self.win.drv_busy and time.time() < deadline:
                QT.processEvents()
                time.sleep(0.01)
        return seen, warn

    def test_fallback_runs_only_after_an_explicit_yes(self):
        seen, warn = self.vendor_fallback_run(True)
        self.assertTrue(seen['thread'] and seen['dialog_hidden'])
        warn.assert_not_called()
        self.assertFalse(self.win.drv_busy)
        self.win.close()

    def test_fallback_declined_reports_failure(self):
        _, warn = self.vendor_fallback_run(False)
        warn.assert_called_once()
        self.assertFalse(self.win.drv_busy)
        self.win.close()

    def test_closing_the_window_releases_a_waiting_worker(self):
        out = []
        self.win.drv_closing.set()
        t = threading.Thread(target=lambda: out.append(self.win.ask_vendor_fallback()))
        t.start()
        t.join(3)
        self.assertFalse(t.is_alive())
        self.assertEqual(out, [False])
        self.win.close()

    def test_failed_windows_bind_shows_call_log_and_manual_steps(self):
        _, warn = self.run_install(lambda box: box.defaultButton(),
                                   install_inbox_winusb=dict(side_effect=A.driver.DriverError(
                                       'DiInstallDevice failed: access denied (0x5). Details: C:\\t\\winusb_setup.log')))
        text = warn.call_args.args[2]
        self.assertIn('DiInstallDevice', text)
        self.assertIn('winusb_setup.log', text)
        self.assertIn('Let me pick', text)
        self.assertIn('Download Vendor Driver', text)
        self.assertTrue(self.win.banner.isVisible())
        self.win.close()

    def test_absent_camera_says_plug_it_in_without_manual_steps(self):
        _, warn = self.run_install(lambda box: box.defaultButton(), state='absent',
                                   install_inbox_winusb=dict(side_effect=A.driver.DriverCameraAbsent('Plug it in first')))
        self.assertIn('Plug it in first', warn.call_args.args[2])
        self.assertNotIn('Let me pick', warn.call_args.args[2])

    def test_esc_on_the_busy_dialog_keeps_it_open(self):
        self.win.show()
        with mock.patch.object(A.driver, 'install_inbox_winusb', side_effect=lambda *a: time.sleep(0.3) or 'ok'), \
                mock.patch.object(QMessageBox, 'information'):
            self.win.start_driver_job('windows', 'Setting Up Camera Driver')
            dlg = self.win.drv_dialog
            dlg.reject()                                # what Esc does
            for _ in range(5):
                QT.processEvents()
                time.sleep(0.01)
            self.assertTrue(dlg.isVisible())
            self.assertFalse(self.win.drv_cancel.is_set())
            deadline = time.time() + 5
            while self.win.drv_busy and time.time() < deadline:
                QT.processEvents()
                time.sleep(0.01)
        self.win.close()

    def test_declined_uac_shows_the_plain_message(self):
        _, warn = self.run_install(lambda box: box.defaultButton(),
                                   install_inbox_winusb=dict(side_effect=A.driver.DriverCancelled('Administrator permission was denied')))
        self.assertNotIn('Let me pick', warn.call_args.args[2])

    def test_no_cancel_button_for_the_windows_driver(self):
        self.win.show()
        with mock.patch.object(A.driver, 'install_inbox_winusb', side_effect=lambda *a: time.sleep(0.2) or 'ok'), \
                mock.patch.object(QMessageBox, 'information'):
            self.win.start_driver_job('windows', 'Setting Up Camera Driver')
            self.assertEqual(self.win.drv_dialog.findChildren(QPushButton), [])
            deadline = time.time() + 5
            while self.win.drv_busy and time.time() < deadline:
                QT.processEvents()
                time.sleep(0.01)
        self.win.close()

    def test_remove_action_follows_the_binding(self):
        self.win.act_driver_remove.setVisible(True)
        self.assertFalse(self.win.act_driver_remove.isEnabled())
        self.win.on_log('@binding usbccgp')
        self.assertFalse(self.win.act_driver_remove.isEnabled())
        self.win.on_log('@binding winusb')
        self.assertTrue(self.win.act_driver_remove.isEnabled())
        self.assertEqual(self.win.act_driver_remove.text(), 'Remove Windows Driver Binding…')
        self.win.drv_busy = True
        self.win.update_driver_actions()
        self.assertFalse(self.win.act_driver_remove.isEnabled())

    def test_remove_flow(self):
        self.win.show()
        self.win.binding = 'winusb'
        with mock.patch.object(QMessageBox, 'exec', lambda box: box.defaultButton().click()), \
                mock.patch.object(QMessageBox, 'information') as info, \
                mock.patch.object(A.driver, 'remove_inbox_winusb', return_value='removed') as rm:
            self.win.offer_driver_remove()
            deadline = time.time() + 5
            while self.win.drv_busy and time.time() < deadline:
                QT.processEvents()
                time.sleep(0.01)
        rm.assert_called_once()
        info.assert_called_once()
        self.win.close()

    def test_banner_does_not_imply_the_vendor_download(self):
        texts = ' '.join(l.text() for l in self.win.banner.findChildren(QLabel))
        self.assertNotIn('installed', texts)
        self.assertNotIn('vendor', texts.lower())

    def test_driver_actions_are_in_the_camera_menu(self):
        menus = {m.text(): m.menu().actions() for m in self.win.menuBar().actions()}
        self.assertIn(self.win.act_driver, menus['&Camera'])
        self.assertIn(self.win.act_driver_remove, menus['&Camera'])


if __name__ == '__main__':
    unittest.main()
