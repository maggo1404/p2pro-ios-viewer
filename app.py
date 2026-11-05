"""P2 Pro (Lightning) thermal camera app - Qt GUI with side panel.

Run:  python app.py        (unplug + replug the camera before every start)

Mouse on the image:
  left click   add a temperature cursor
  left drag    draw a rectangle (max / min / average inside)
  right click  delete the cursor / rectangle under the mouse
"""
import sys
if __name__ == '__main__':
    # Elevated --winusb-bind / --winusb-revert / --winusb-diagnose runs: handled before the heavy imports below
    # (only stdlib + winusb are loaded), without Qt and without a window.
    import winusb
    _rc = winusb.run_cli(sys.argv[1:])
    if _rc is not None:
        sys.exit(_rc)

import json
import os
import threading
import time

import cv2
import numpy as np
from html import escape

from PySide6.QtCore import QByteArray, QEvent, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QImage, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressDialog,
                               QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox, QSplitter, QToolBar, QVBoxLayout,
                               QWidget)

import driver
import exporting
import ui
from camera import IMG_H, IMG_W, Camera, CameraError
from processing import DEFAULT_CALIB, PALETTES, Processor, orient_point, unorient_point
from streaming import Recorder, RtspServer, find_ffmpeg

from paths import APP_DIR
SETTINGS_FILE = os.path.join(APP_DIR, 'settings.json')
S = 3                                   # output scale: 256x196 -> 768x588 (196x256 when rotated)
BAR_W = 72                              # colour bar area on the right of the output image
CANVAS = (30, 30, 30)                   # neutral surround of the image, dark in both appearances (it is part of the picture)
FPS = 25


def letterbox(img, size):
    """fit a BGR image into size (w, h) keeping aspect ratio, black borders"""
    W, H = size
    h, w = img.shape[:2]
    if (w, h) == (W, H):
        return img
    k = min(W / w, H / h)
    nw, nh = max(int(w * k), 1), max(int(h * k), 1)
    out = np.zeros((H, W, 3), np.uint8)
    x, y = (W - nw) // 2, (H - nh) // 2
    out[y:y + nh, x:x + nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    return out


def c2u(t, f):                          # C -> display unit
    return t * 9 / 5 + 32 if f else t


def u2c(t, f):
    return (t - 32) * 5 / 9 if f else t


_FONTS = {}


def outlined_text(p, x, y, s, color=QColor(255, 255, 255), size=11, bold=True):
    key = (size, bold)
    if key not in _FONTS:
        font = QFont(QApplication.font())   # the system UI font of the platform
        font.setPointSize(size)
        font.setWeight(QFont.Bold if bold else QFont.Normal)
        _FONTS[key] = font
    p.setFont(_FONTS[key])
    p.setPen(QColor(0, 0, 0))
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1)):
        p.drawText(QPointF(x + dx, y + dy), s)
    p.setPen(color)
    p.drawText(QPointF(x, y), s)


def cross(p, x, y, color, r=7):
    for pen in (QPen(QColor(0, 0, 0), 3), QPen(color, 1.4)):
        p.setPen(pen)
        p.drawLine(QPointF(x - r, y), QPointF(x + r, y))
        p.drawLine(QPointF(x, y - r), QPointF(x, y + r))


# =====================================================================================
class ImageView(QWidget):
    """Shows the composed frame scaled to fit; maps mouse to sensor pixels."""
    clicked = Signal(int, int)             # sensor x, y
    rect_drawn = Signal(int, int, int, int)
    right_clicked = Signal(int, int)

    def __init__(self):
        super().__init__()
        self.img = None
        self.target = QRectF()
        self.drag = None                  # (start QPointF, current QPointF) in widget coords
        self.setMinimumSize(420, 320)
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)
        self.message = 'Connecting…'
        self.setAccessibleName('Thermal image')
        self.setAccessibleDescription('Click to add a temperature cursor, drag to draw a rectangle, right-click to delete.')
        self.sensor_w, self.sensor_h = IMG_W, IMG_H   # size of the displayed (possibly rotated) image

    def set_image(self, qimg):
        self.img = qimg
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(*CANVAS))
        if self.img is None:
            p.setPen(QColor(200, 200, 200))
            font = QFont(self.font())
            font.setPointSize(12)
            p.setFont(font)
            p.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap, self.message)
            return
        iw, ih = self.img.width(), self.img.height()
        k = min(self.width() / iw, self.height() / ih)
        w, h = iw * k, ih * k
        self.target = QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawImage(self.target, self.img)
        if self.drag:
            p.setPen(QPen(QColor(0, 255, 255), 1, Qt.DashLine))
            p.drawRect(QRectF(self.drag[0], self.drag[1]).normalized())

    def _sensor(self, pos, clamp=False):
        if self.img is None or self.target.width() == 0:
            return None
        k = self.img.width() / self.target.width()
        cx, cy = (pos.x() - self.target.x()) * k, (pos.y() - self.target.y()) * k
        sw, sh = self.sensor_w, self.sensor_h
        if not clamp and not (0 <= cx < sw * S and 0 <= cy < sh * S):
            return None
        return min(max(int(cx // S), 0), sw - 1), min(max(int(cy // S), 0), sh - 1)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self._sensor(e.position()):
            self.drag = (e.position(), e.position())

    def mouseMoveEvent(self, e):
        if self.drag:
            self.drag = (self.drag[0], e.position())
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.RightButton:
            s = self._sensor(e.position())
            if s:
                self.right_clicked.emit(*s)
        elif e.button() == Qt.LeftButton and self.drag:
            a, b = self.drag
            self.drag = None
            self.update()
            if abs(a.x() - b.x()) < 5 and abs(a.y() - b.y()) < 5:
                s = self._sensor(b)
                if s:
                    self.clicked.emit(*s)
            else:
                s0, s1 = self._sensor(a, True), self._sensor(b, True)
                self.rect_drawn.emit(min(s0[0], s1[0]), min(s0[1], s1[1]), max(s0[0], s1[0]), max(s0[1], s1[1]))


VENDOR_FALLBACK_TEXT = (
    'Windows (pnputil) rejected the vendor driver because it is unsigned.\n\n'
    'The alternative is to run the vendor\u2019s own installer tool as administrator. According to static analysis '
    'of that tool (it has not been run here), it adds a self-signed certificate named \u201clibwdi\u201d to this '
    'computer\u2019s Trusted Root and Trusted Publishers certificate stores. The certificate stays there until you '
    'remove it by hand (certlm.msc). The tool may also install drivers for other camera models.\n\n'
    'Alternative: cancel and choose \u201cUse Windows Driver\u201d, which needs no certificate.')


class BusyDialog(QProgressDialog):
    """Progress dialog that Esc and the close button can't dismiss while keep_open is set (a job past the point of
    no return keeps running, so its progress must stay visible)."""
    keep_open = False

    def reject(self):
        if not self.keep_open:
            super().reject()

    def closeEvent(self, e):
        if self.keep_open:
            e.ignore()
        else:
            super().closeEvent(e)


# =====================================================================================
class MainWindow(QMainWindow):
    theme_key = None
    log_signal = Signal(str)
    drv_status = Signal(str)
    drv_progress = Signal(int, int)
    drv_done = Signal(bool, str)
    drv_confirm = Signal(object)            # (threading.Event, result dict): the worker waits for the UI thread

    def __init__(self):
        super().__init__()
        self.setWindowTitle('P2 Pro Thermal (Lightning)')
        self.proc = Processor()
        self.cam = None
        self.connecting = False
        self.points, self.rects = [], []
        self.cal_mode = False
        self.cal_points = []
        self.auto_range = None
        self.last_range = (20.0, 30.0)
        self.temp = self.diff = None
        self.last_n = 0
        self.last_shutter = 0
        self.shutter_busy = False
        self.recorder = self.rtsp = None
        self.fps_count, self.fps_t, self.fps = 0, time.time(), 0.0
        self.proc_ms = 0.0
        self.drv_busy = False
        self.drv_cancel = threading.Event()
        self.drv_dialog = None
        self.drv_offered = False
        self.drv_missing = False
        self.drv_connect_after = True
        self.drv_cancellable = False
        self.drv_job = 'vendor'                 # what the progress dialog is for: vendor / windows / remove
        self.binding = 'absent'                 # driver of the connected camera, see driver.binding_state()
        self.conn_text, self.conn_tip = 'Not Connected', ''

        self.view = ImageView()
        self.view.clicked.connect(self.on_click)
        self.view.rect_drawn.connect(lambda *r: self.rects.append(r))
        self.view.right_clicked.connect(self.on_right_click)

        self.build_actions()
        panel = self.build_panel()
        self.scroll = QScrollArea()
        self.scroll.setWidget(panel)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        margins = panel.layout().contentsMargins()      # collapsed sections count too: they must fit once opened
        content_w = max(s.body.layout().minimumSize().width() for s in self.sections.values())
        self.scroll.setMinimumWidth(max(panel.minimumSizeHint().width(), content_w + margins.left() + margins.right())
                                    + self.scroll.verticalScrollBar().sizeHint().width())
        self.act_inspector.toggled.connect(self.scroll.setVisible)
        content = QWidget()                 # image column: optional notice + image
        lay = QVBoxLayout(content)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.build_banner())
        lay.addWidget(self.view, 1)
        self.split = QSplitter()
        self.split.addWidget(content)
        self.split.addWidget(self.scroll)
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 0)
        self.split.setCollapsible(0, False)
        self.split.setCollapsible(1, False)
        self.setCentralWidget(self.split)
        self.status = QLabel()
        self.statusBar().addWidget(self.status, 1)
        self.log_signal.connect(self.on_log)
        self.drv_closing = threading.Event()            # set when the window closes: releases a waiting worker
        self.drv_confirm.connect(self.on_drv_confirm)
        self.drv_status.connect(self.on_drv_status)
        self.drv_progress.connect(self.on_drv_progress)
        self.drv_done.connect(self.on_drv_done)
        self.resize(1200, 760)
        self.split.setSizes([860, 340])
        self.load_settings()
        self.apply_theme()
        self.update_actions()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(5)
        if driver.is_windows():
            QTimer.singleShot(100, self.check_driver)       # connecting is started afterwards if the driver is fine
        else:
            QTimer.singleShot(200, self.connect_camera)

    # ------------------------------------------------------------------ commands
    def build_actions(self):
        """Every command is in the menu bar; the frequent ones are also in the toolbar."""
        def action(text, fn, key=None, short=None, tip=None):
            a = QAction(text, self)
            a.triggered.connect(lambda _checked=False: fn())
            if key:
                a.setShortcut(QKeySequence(key))
            if short:
                a.setIconText(short)
            if tip:
                a.setToolTip(tip)
            return a

        self.act_snapshot = action('Snapshot', self.snapshot, 'S', tip='Quick save (S): screenshot + per-pixel temperature '
                                   'data, auto-named in the snapshots folder, in the formats chosen under Output')
        self.act_export = action('Export…', self.export_dialog, 'Ctrl+E')
        self.act_rec = action('Start Recording', self.toggle_record, short='Record')
        self.act_rtsp = action('Start Stream', self.toggle_rtsp, short='Stream', tip='Start the RTSP stream')
        quit_ = action('Quit', self.close, 'Ctrl+Q')
        quit_.setMenuRole(QAction.QuitRole)
        if not find_ffmpeg():             # recording falls back to OpenCV; RTSP needs ffmpeg
            self.act_rtsp.setEnabled(False)
            self.act_rtsp.setToolTip('ffmpeg not found')
            self.act_rec.setToolTip('ffmpeg not found: recording uses OpenCV (MPEG-4, larger files)')
        m = self.menuBar().addMenu('&File')
        m.addActions([self.act_snapshot, self.act_export])
        m.addSeparator()
        m.addActions([self.act_rec, self.act_rtsp])
        m.addSeparator()
        m.addAction(quit_)

        self.act_inspector = QAction('Show Inspector', self, checkable=True, checked=True)
        self.act_inspector.setIconText('Inspector')
        self.act_inspector.setShortcut(QKeySequence('Ctrl+I'))
        self.act_inspector.setToolTip('Show or hide the settings panel (Ctrl+I)')
        act_full = action('Full Screen', lambda: self.setWindowState(self.windowState() ^ Qt.WindowFullScreen),
                          QKeySequence(QKeySequence.FullScreen).toString())
        m = self.menuBar().addMenu('&View')
        m.addAction(self.act_inspector)
        m.addAction(act_full)
        m.addSeparator()
        m.addAction(action('Rotate 90° Clockwise', self.rotate, 'R'))
        m.addAction(action('Switch Auto / Fixed Scale',
                           lambda: (self.rb_fixed if self.rb_auto.isChecked() else self.rb_auto).setChecked(True), 'A'))

        self.act_connect = action('Connect', self.connect_camera, tip='Unplug the camera and plug it back in, then connect')
        self.act_shutter = action('Shutter Calibration', self.shutter, 'C', short='Shutter',
                                  tip='Flat-field shutter calibration (C)')
        self.act_cal = action('Calibrate Temperature…', self.start_calibration)
        self.act_cal_cancel = action('Cancel Calibration', self.cancel_calibration, 'Escape')
        self.act_cal_cancel.setEnabled(False)
        self.act_driver = action('Install Camera Driver…', self.offer_driver_install,
                                 tip='Use the driver that comes with Windows, or download the vendor driver (needs admin rights)')
        self.act_driver.setVisible(driver.is_windows())
        self.act_driver_remove = action('Remove Windows Driver Binding…', self.offer_driver_remove,
                                        tip='Give the camera back to the driver Windows chooses by itself (needs admin rights)')
        self.act_driver_remove.setVisible(driver.is_windows())
        self.act_driver_remove.setEnabled(False)
        m = self.menuBar().addMenu('&Camera')
        m.addActions([self.act_connect, self.act_shutter])
        m.addSeparator()
        m.addActions([self.act_cal, self.act_cal_cancel, action('Reset Calibration', self.reset_calibration)])
        m.addSeparator()
        m.addActions([self.act_driver, self.act_driver_remove])

        m = self.menuBar().addMenu('&Measure')
        m.addAction(action('Clear Cursors', lambda: self.points.clear()))
        m.addAction(action('Clear Rectangles', lambda: self.rects.clear()))
        m.addAction(action('Clear All', self.clear_all, 'Delete'))

        tb = QToolBar('Main')
        tb.setObjectName('main_toolbar')
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setIconSize(QSize(18, 18))
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tb.addActions([self.act_connect, self.act_shutter])
        tb.addSeparator()
        tb.addActions([self.act_snapshot, self.act_rec, self.act_rtsp])
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        self.chip = QLabel()                # connection + recording state: stays visible with the panel hidden
        self.chip.setContentsMargins(0, 0, 12, 0)
        tb.addWidget(self.chip)
        tb.addAction(self.act_inspector)
        self.addToolBar(tb)
        self.setContextMenuPolicy(Qt.NoContextMenu)
        self.setUnifiedTitleAndToolBarOnMac(True)

    def build_banner(self):
        """Non-modal notice above the image; replaces the alert that used to appear on launch."""
        self.banner = QFrame()
        self.banner.setObjectName('banner')
        h = QHBoxLayout(self.banner)
        h.setContentsMargins(12, 6, 8, 6)
        h.addWidget(QLabel('The camera needs a USB driver to work with this app.'), 1)
        b = QPushButton('Set Up Driver…')
        b.clicked.connect(self.offer_driver_install)
        h.addWidget(b)
        b = QPushButton('Not Now')
        b.clicked.connect(self.banner.hide)
        h.addWidget(b)
        self.banner.hide()
        return self.banner

    def apply_theme(self):
        pal = QApplication.palette()
        self.theme_key = ui.scheme_key(pal)
        self.setStyleSheet(ui.stylesheet(pal))
        for a, name in ((self.act_connect, 'connect'), (self.act_shutter, 'shutter'), (self.act_snapshot, 'snapshot'),
                        (self.act_rtsp, 'stream'), (self.act_inspector, 'inspector')):
            a.setIcon(ui.icon(name, pal))
        self.update_status()

    def changeEvent(self, e):
        # the system appearance can change while the app is running
        if (self.theme_key is not None and e.type() in (QEvent.ApplicationPaletteChange, QEvent.PaletteChange)
                and ui.scheme_key(QApplication.palette()) != self.theme_key):
            self.apply_theme()
        super().changeEvent(e)

    # ------------------------------------------------------------------ panel
    def build_panel(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(10, 6, 10, 10)
        v.setSpacing(6)
        self.sections = {}

        def section(key, title, expanded, layout_cls=QFormLayout):
            s = ui.Section(title, expanded)
            if self.sections:
                line = QFrame()
                line.setFrameShape(QFrame.HLine)
                line.setProperty('role', 'separator')
                v.addWidget(line)
            self.sections[key] = s
            v.addWidget(s)
            return layout_cls(s.body)

        def note(text):
            lab = QLabel(text)
            lab.setWordWrap(True)
            lab.setProperty('role', 'secondary')
            return lab

        # --- measurement
        f = section('measurement', 'Measurement', True)
        f.addRow(note('Click the image to add a cursor, drag to draw a rectangle, right-click to delete.'))
        self.cb_hotcold = QCheckBox('Mark hottest and coldest spots')
        self.cb_hotcold.setChecked(True)
        self.cb_center = QCheckBox('Center crosshair')
        f.addRow(self.cb_hotcold)
        f.addRow(self.cb_center)
        seg, (_celsius, self.cb_fahrenheit) = ui.segmented('°C', '°F')
        self.cb_fahrenheit.toggled.connect(self.units_changed)
        f.addRow('Unit', seg)
        b = QPushButton('Clear All')
        b.setToolTip('Remove every cursor and rectangle (Del)')
        b.clicked.connect(self.clear_all)
        f.addRow(b)

        # --- display
        f = section('display', 'Display', True)
        self.cmb_palette = QComboBox()
        self.cmb_palette.addItems(list(PALETTES))
        self.cmb_palette.currentTextChanged.connect(lambda t: setattr(self.proc, 'palette', t))
        f.addRow('Palette', self.cmb_palette)
        row = QHBoxLayout()
        seg, (self.rb_auto, self.rb_fixed) = ui.segmented('Auto', 'Fixed')
        row.addWidget(seg, 1)
        b = QPushButton('Lock Current')
        b.setToolTip('Switch to fixed scale using the current auto range')
        b.clicked.connect(self.lock_range)
        row.addWidget(b)
        f.addRow('Scale', row)
        self.sp_min, self.sp_max = QDoubleSpinBox(), QDoubleSpinBox()
        for sp, val, name in ((self.sp_min, 20, 'Range minimum'), (self.sp_max, 35, 'Range maximum')):
            sp.setRange(-100, 1000)
            sp.setDecimals(1)
            sp.setSingleStep(0.5)
            sp.setValue(val)
            sp.setAccessibleName(name)
            sp.setEnabled(False)                # auto scale drives these; editable when the scale is fixed
            self.rb_fixed.toggled.connect(sp.setEnabled)
        row = QHBoxLayout()
        row.addWidget(self.sp_min)
        row.addWidget(QLabel('to'))
        row.addWidget(self.sp_max)
        f.addRow('Range', row)
        self.cb_smooth_range = QCheckBox('Smooth auto range')
        self.cb_smooth_range.setToolTip('Less flicker: the auto range follows the scene gradually')
        self.cb_smooth_range.setChecked(True)
        f.addRow('', self.cb_smooth_range)
        self.cmb_contrast = QComboBox()
        self.cmb_contrast.addItems(Processor.CONTRAST)
        self.cmb_contrast.currentTextChanged.connect(lambda t: setattr(self.proc, 'contrast', t))
        f.addRow('Contrast', self.cmb_contrast)
        self.cmb_upscale = QComboBox()
        self.cmb_upscale.addItems([u for u in Processor.UPSCALE
                                   if not u.startswith('Super') or self.proc.sr.available(int(u.split('x')[1][0]))])
        self.cmb_upscale.currentTextChanged.connect(lambda t: setattr(self.proc, 'upscale', t))
        f.addRow('Upscaling', self.cmb_upscale)
        self.cmb_rotation = QComboBox()
        self.cmb_rotation.addItems(['0°', '90°', '180°', '270°'])
        self.cmb_rotation.setToolTip('Rotate the image clockwise (R = next step)')
        self.cmb_rotation.currentIndexChanged.connect(lambda _i: self.orientation_widgets_changed())
        f.addRow('Rotation', self.cmb_rotation)
        row = QHBoxLayout()
        self.cb_flip_h, self.cb_flip_v = QCheckBox('Horizontal'), QCheckBox('Vertical')
        self.cb_flip_h.setAccessibleName('Mirror horizontal')
        self.cb_flip_v.setAccessibleName('Mirror vertical')
        self.cb_flip_h.setToolTip('Mirror left/right on screen (applied after rotation)')
        self.cb_flip_v.setToolTip('Mirror top/bottom on screen (applied after rotation)')
        self.cb_flip_h.toggled.connect(lambda _b: self.orientation_widgets_changed())
        self.cb_flip_v.toggled.connect(lambda _b: self.orientation_widgets_changed())
        row.addWidget(self.cb_flip_h)
        row.addWidget(self.cb_flip_v)
        f.addRow('Mirror', row)

        # --- enhancement
        f = section('enhancement', 'Image Enhancement', False)
        f.addRow(note('Denoise, stripe removal and bad pixel repair also clean the temperature readings; '
                      'the others only change the picture.'))
        self.enh = {}

        def enh_row(key, label, attr_on, attr_val=None, lo=0, hi=1, tip=''):
            cb = QCheckBox(label)
            cb.setToolTip(tip)
            cb.toggled.connect(lambda b: setattr(self.proc, attr_on, b))
            sl = None
            if attr_val:
                sl = QSlider(Qt.Horizontal)
                sl.setRange(0, 100)
                sl.setMinimumWidth(70)
                sl.setToolTip('Strength')
                sl.setAccessibleName(label + ' strength')
                sl.valueChanged.connect(lambda x: setattr(self.proc, attr_val, lo + (hi - lo) * x / 100))
                cb.toggled.connect(sl.setEnabled)
            f.addRow(cb, sl) if sl else f.addRow(cb)
            self.enh[key] = (cb, sl, attr_on, attr_val, lo, hi)
        enh_row('bad', 'Bad pixel repair', 'bad_pixels', tip='Replace stuck/blinking pixels with their neighbours')
        enh_row('stripe', 'Stripe removal', 'destripe', 'destripe_strength', 0, 1,
                'Remove row/column fixed-pattern noise (vertical/horizontal lines)')
        enh_row('denoise', 'Temporal denoise', 'denoise', 'denoise_strength', 0, 0.95,
                'Average over time where the scene is still (motion adaptive)')
        enh_row('dde', 'Detail enhancement', 'dde', 'dde_strength', 1, 5,
                'Digital detail enhancement (DDE): boost fine detail while compressing the big temperature range')
        enh_row('sharpen', 'Sharpen', 'sharpen', 'sharpen_strength', 0, 2, 'Unsharp mask after upscaling')

        # --- calibration
        f = section('calibration', 'Calibration', False)
        self.sp_autoshutter = QSpinBox()
        self.sp_autoshutter.setRange(0, 600)
        self.sp_autoshutter.setValue(60)
        self.sp_autoshutter.setSuffix(' s')
        self.sp_autoshutter.setSpecialValueText('Off')
        self.sp_autoshutter.setToolTip('Run the flat-field shutter calibration automatically at this interval')
        f.addRow('Auto Shutter', self.sp_autoshutter)
        row = QHBoxLayout()
        self.btn_cal = QPushButton('Calibrate…')
        self.btn_cal.setToolTip('Click a spot whose real temperature you know, then enter it.\n'
                                'One point fixes the offset, two points (different temperatures) also fix the gain.')
        self.btn_cal.clicked.connect(self.start_calibration)
        b = QPushButton('Reset')
        b.setToolTip('Back to the default calibration')
        b.clicked.connect(self.reset_calibration)
        row.addWidget(self.btn_cal)
        row.addWidget(b)
        f.addRow(row)
        self.cal_label = note('')
        f.addRow(self.cal_label)

        # --- output
        f = section('output', 'Output', False)
        self.cmb_snap_image = QComboBox()
        self.cmb_snap_image.addItems([n for n, _ in exporting.IMAGE_FORMATS])
        self.cmb_snap_image.setToolTip('Screenshot format (includes overlays and colour bar)')
        f.addRow('Image', self.cmb_snap_image)
        self.cmb_snap_data = QComboBox()
        self.cmb_snap_data.addItems([n for n, _ in exporting.DATA_FORMATS])
        self.cmb_snap_data.setToolTip('Per-pixel temperatures (in the selected unit, same pixel grid as the '
                                      'unscaled image): CSV / XML text, or float32 TIFF for ImageJ / Python')
        f.addRow('Data', self.cmb_snap_data)
        self.btn_export = QPushButton('Export…')
        self.btn_export.setToolTip('Choose the file name and folder; the temperature data is saved next to the image '
                                   'with the same name (Ctrl+E)')
        self.btn_export.clicked.connect(self.export_dialog)
        f.addRow(self.btn_export)
        self.cb_overlay_out = QCheckBox('Record and stream overlays')
        self.cb_overlay_out.setToolTip('Include cursors and labels in the recording and the stream')
        self.cb_overlay_out.setChecked(True)
        f.addRow(self.cb_overlay_out)
        self.rtsp_url = QLineEdit()
        self.rtsp_url.setReadOnly(True)
        self.rtsp_url.setAccessibleName('Stream URL')
        self.rtsp_url.hide()                # only relevant while streaming
        self.rtsp_row_label = QLabel('Stream URL')
        self.rtsp_row_label.hide()
        f.addRow(self.rtsp_row_label, self.rtsp_url)
        for lay in (s.body.layout() for s in self.sections.values()):
            lay.setRowWrapPolicy(QFormLayout.DontWrapRows)
            lay.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        v.addStretch(1)
        self.update_cal_label()
        return w

    # ------------------------------------------------------------------ driver
    def check_driver(self, connect=True):
        self.drv_connect_after = connect

        def work():
            state = driver.binding_state()
            self.log_signal.emit('@binding ' + state)
            ok = driver.is_driver_installed()
            self.log_signal.emit('@driver ' + ('missing' if ok is False else 'ok'))
        threading.Thread(target=work, daemon=True).start()

    def refresh_binding(self):
        if not driver.is_windows():
            return
        threading.Thread(target=lambda: self.log_signal.emit('@binding ' + driver.binding_state()), daemon=True).start()

    def update_driver_actions(self):
        self.act_driver.setEnabled(not self.drv_busy)
        self.act_driver_remove.setEnabled(not self.drv_busy and self.binding == 'winusb')

    def offer_driver_install(self):
        if self.drv_busy:
            return
        state = self.binding                                # cached: SetupAPI is not queried on the UI thread
        box = QMessageBox(QMessageBox.Question, 'Install Camera Driver',
                          'Which driver should the camera use?', QMessageBox.Cancel, self)
        text = ('Windows driver (recommended): uses the WinUSB driver that is already part of Windows. Nothing is downloaded. '
                'Windows asks for administrator permission once, and the camera (USB 0BDA:5840) switches from the '
                'generic USB composite driver to WinUSB; only the camera\u2019s own driver assignment changes. '
                'To undo it, choose Camera > Remove Windows Driver Binding.\n\n'
                'Vendor driver: downloads the libusbK driver from thermalmaster.com, in the "Temp Master (P-Serie)" '
                'package, about 55 MB. Windows asks for administrator permission.')
        if state == 'libusbk':
            text += ('\n\nThe camera currently uses the vendor driver (libusbK). The Windows driver replaces it; '
                     'the Thermal Master app may need the vendor driver back.')
        elif state == 'winusb':
            text += '\n\nThe camera already uses the Windows driver.'
        elif state == 'absent':
            text += '\n\nThe Windows driver can only be set while the camera is plugged in.'
        box.setInformativeText(text)
        windows = box.addButton('Use Windows Driver', QMessageBox.AcceptRole)
        vendor = box.addButton('Download Vendor Driver…', QMessageBox.ActionRole)
        box.setDefaultButton(windows)
        box.exec()
        chosen = box.clickedButton()
        if chosen == windows:
            self.start_driver_job('windows', 'Setting Up Camera Driver', replace_libusbk=(state == 'libusbk'))
        elif chosen == vendor:
            self.start_driver_job('vendor', 'Installing Camera Driver')

    def offer_driver_remove(self):
        if self.drv_busy:
            return
        box = QMessageBox(QMessageBox.Question, 'Remove Windows Driver Binding',
                          'Remove the Windows driver binding?', QMessageBox.Cancel, self)
        box.setInformativeText('The camera goes back to the driver Windows chooses by itself (or the vendor driver, '
                               'if it is installed). Windows asks for administrator permission. Afterwards, unplug '
                               'and replug the camera. The app needs a driver again to reach the camera.')
        remove = box.addButton('Remove', QMessageBox.AcceptRole)
        box.setDefaultButton(remove)
        box.exec()
        if box.clickedButton() == remove:
            self.start_driver_job('remove', 'Removing Windows Driver Binding')

    def start_driver_job(self, job, title, replace_libusbk=False):
        self.banner.hide()
        self.drv_busy = True
        self.drv_job = job
        self.update_driver_actions()
        self.drv_cancel.clear()
        # only the download can be cancelled; the Windows driver steps end at the administrator prompt
        dlg = BusyDialog('Starting…', 'Cancel' if job == 'vendor' else None, 0, 0, self)
        dlg.setWindowTitle(title)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        self.drv_cancellable = job == 'vendor'
        dlg.keep_open = job != 'vendor'
        dlg.canceled.connect(self.on_drv_canceled)
        dlg.show()
        self.drv_dialog = dlg

        def work():
            try:
                if job == 'vendor':
                    msg = driver.install_from_download(self.drv_status.emit, self.drv_progress.emit, self.drv_cancel,
                                                       self.ask_vendor_fallback)
                elif job == 'windows':
                    msg = driver.install_inbox_winusb(self.drv_status.emit, replace_libusbk)
                else:
                    msg = driver.remove_inbox_winusb(self.drv_status.emit)
                self.drv_done.emit(True, msg)
            except driver.DriverCancelled as e:
                self.drv_done.emit(False, '' if str(e) == 'cancelled' else str(e))
            except driver.DriverError as e:
                msg = str(e)
                if job == 'windows' and not isinstance(e, (driver.DriverNeedsConfirmation, driver.DriverCameraAbsent)):
                    msg += '\n\n' + driver.fallback_text()
                self.drv_done.emit(False, msg)
            except Exception as e:
                self.drv_done.emit(False, f'{type(e).__name__}: {e}')
        threading.Thread(target=work, daemon=True).start()

    def ask_vendor_fallback(self):
        """Worker thread: asks the UI thread whether the vendor installer may run. Blocks until answered; False if
        the window closes meanwhile (no Qt call happens here)."""
        done, result = threading.Event(), {'ok': False}
        self.drv_confirm.emit((done, result))
        while not done.wait(0.2):
            if self.drv_closing.is_set():
                return False
        return result['ok']

    def make_fallback_box(self):
        box = QMessageBox(QMessageBox.Warning, 'Run the Vendor Installer?', 'Windows rejected the driver.',
                          QMessageBox.NoButton, self)
        box.setInformativeText(VENDOR_FALLBACK_TEXT)
        box.addButton(QMessageBox.Cancel)
        run = box.addButton('Run Vendor Installer', QMessageBox.AcceptRole)
        box.setDefaultButton(QMessageBox.Cancel)
        box.setEscapeButton(QMessageBox.Cancel)
        return box, run

    def on_drv_confirm(self, req):
        """UI thread: shows the warning (the busy dialog is hidden meanwhile) and releases the worker."""
        done, result = req
        try:
            if self.drv_closing.is_set():
                return
            dlg = self.drv_dialog
            if dlg:
                dlg.hide()
            box, run = self.make_fallback_box()
            box.exec()
            result['ok'] = box.clickedButton() is run
            if dlg and self.drv_dialog is dlg:
                dlg.show()
        finally:
            done.set()

    def on_drv_canceled(self):
        """Cancel (or Esc) on the progress dialog: stops the download; otherwise the dialog just stays."""
        if self.drv_cancellable:
            self.drv_cancel.set()

    def on_drv_status(self, s):
        if self.drv_dialog:
            self.drv_dialog.setLabelText(s)
            if 'administrator' in s or 'vendor' in s:       # past the point where cancelling is possible
                self.drv_cancellable = False
                self.drv_dialog.keep_open = True
                self.drv_dialog.setCancelButton(None)
                self.drv_dialog.setRange(0, 0)

    def on_drv_progress(self, done, total):
        if self.drv_dialog and total:
            self.drv_dialog.setRange(0, total)
            self.drv_dialog.setValue(min(done, total))
            self.drv_dialog.setLabelText(f'Downloading… {done / 1e6:.1f} of {total / 1e6:.1f} MB')

    def on_drv_done(self, ok, msg):
        self.drv_busy = False
        self.update_driver_actions()
        if self.drv_dialog:
            self.drv_dialog.keep_open = False
            self.drv_dialog.canceled.disconnect()           # close() would emit canceled()
            self.drv_dialog.close()
            self.drv_dialog = None
        self.refresh_binding()
        QTimer.singleShot(4000, self.refresh_binding)       # the device node restarts; unplugging changes it again
        if ok:
            if self.drv_job != 'remove':
                self.drv_missing = False
            QMessageBox.information(self, 'Camera Driver', msg)
        else:
            self.banner.setVisible(self.drv_missing)
            failed = 'The driver binding could not be removed.' if self.drv_job == 'remove' \
                else 'The driver could not be installed.'
            if msg:
                QMessageBox.warning(self, 'Camera Driver', failed + '\n\n' + msg)

    # ------------------------------------------------------------------ camera
    def connect_camera(self):
        if self.connecting:
            return
        if self.cam:
            self.cam.close()
            self.cam = None
        self.connecting = True
        self.refresh_binding()
        self.act_connect.setEnabled(False)
        self.set_connection('Connecting…', 'Takes about 6 seconds')
        self.view.img = None
        self.view.message = 'Connecting to the camera…'
        self.view.update()

        def work():
            try:
                cam = Camera(log=self.log_signal.emit)
                cam.shutter_calibrate()
                self.cam = cam
                self.log_signal.emit('@connected')
            except CameraError as e:
                self.log_signal.emit('@failed ' + str(e))
            except Exception as e:
                self.log_signal.emit(f'@failed {type(e).__name__}: {e}')
        threading.Thread(target=work, daemon=True).start()

    def on_log(self, s):
        if s == '@driver missing':
            self.drv_offered = True
            self.drv_missing = True
            self.banner.show()
            if self.drv_connect_after:
                self.connect_camera()                   # still try: the driver check can be wrong
        elif s.startswith('@binding'):
            self.binding = s[9:]
            self.update_driver_actions()
        elif s == '@driver ok':
            if self.drv_connect_after:
                self.connect_camera()
        elif s == '@connected':
            self.connecting = False
            self.act_connect.setEnabled(True)
            self.set_connection('Connected')
            self.last_shutter = time.time()
            self.proc.reset_temporal()
        elif s.startswith('@failed'):
            self.connecting = False
            self.act_connect.setEnabled(True)
            self.set_connection('Not Connected', s[8:])
            self.statusBar().showMessage('Not connected: ' + s[8:], 8000)
            self.view.message = 'No camera.\n\n' + s[8:] + '\n\nUnplug the camera, plug it back in, then choose Connect.'
            self.view.update()
            if 'camera not found' in s and driver.is_windows() and not self.drv_offered and not self.drv_busy:
                self.drv_offered = True
                self.check_driver(connect=False)
        else:
            print(s)

    def set_connection(self, text, tip=''):
        self.conn_text, self.conn_tip = text, tip
        self.update_actions()
        self.update_status()

    def update_actions(self):
        """commands that have nothing to act on are disabled instead of silently doing nothing"""
        has_frame = self.temp is not None
        self.act_shutter.setEnabled(self.cam is not None)
        for w_ in (self.act_snapshot, self.act_export, self.act_cal, self.btn_export, self.btn_cal):
            w_.setEnabled(has_frame)

    def shutter(self):
        if not self.cam or self.shutter_busy:
            return
        self.shutter_busy = True

        def work():
            try:
                self.cam.shutter_calibrate()
            finally:
                self.shutter_busy = False
        threading.Thread(target=work, daemon=True).start()
        self.last_shutter = time.time()

    # ------------------------------------------------------------------ main loop
    def tick(self):
        cam = self.cam
        if cam and not cam.run and not self.connecting:
            self.cam = None
            self.log_signal.emit('@failed camera disconnected')
            return
        if not cam or cam.ref is None or cam.calibrating or cam.nframe == self.last_n:
            return
        fr, self.last_n = cam.frame, cam.nframe
        t0 = time.perf_counter()
        self.diff, self.temp = self.proc.temperature(fr, cam.ref, cam.ref_n)
        qimg, clean = self.compose(self.temp)
        self.view.set_image(qimg)
        if self.recorder or self.rtsp:
            out = self.qimage_to_bgr(qimg) if self.cb_overlay_out.isChecked() else clean
            if self.recorder:
                self.recorder.push(letterbox(out, self.recorder.size))
            if self.rtsp:
                if self.rtsp.alive:
                    self.rtsp.push(letterbox(out, self.rtsp.size))
                else:
                    self.toggle_rtsp()
                    QMessageBox.warning(self, 'RTSP', 'RTSP stream stopped unexpectedly (see console).')
        self.proc_ms = 0.9 * self.proc_ms + 0.1 * (time.perf_counter() - t0) * 1000
        self.fps_count += 1
        now = time.time()
        if now - self.fps_t >= 1:
            self.fps, self.fps_count, self.fps_t = self.fps_count / (now - self.fps_t), 0, now
            self.update_actions()
            self.update_status()
        iv = self.sp_autoshutter.value()
        if iv and now - self.last_shutter > iv and not self.cal_mode:
            self.shutter()

    def current_range(self, t):
        f = self.cb_fahrenheit.isChecked()
        if self.rb_fixed.isChecked():
            lo, hi = u2c(self.sp_min.value(), f), u2c(self.sp_max.value(), f)
            if hi - lo < 0.2:
                hi = lo + 0.2
            return lo, hi
        lo, hi = float(t.min()), float(t.max())
        if hi - lo < 0.5:
            m = (lo + hi) / 2
            lo, hi = m - 0.25, m + 0.25
        if self.cb_smooth_range.isChecked() and self.auto_range:
            a = 0.3
            lo, hi = self.auto_range[0] * (1 - a) + lo * a, self.auto_range[1] * (1 - a) + hi * a
            lo, hi = min(lo, float(t.min())), max(hi, float(t.max()))   # never clip the extremes
        self.auto_range = (lo, hi)
        if not self.sp_min.hasFocus() and not self.sp_max.hasFocus():
            self.sp_min.blockSignals(True); self.sp_max.blockSignals(True)
            self.sp_min.setValue(c2u(lo, f)); self.sp_max.setValue(c2u(hi, f))
            self.sp_min.blockSignals(False); self.sp_max.blockSignals(False)
        return lo, hi

    def compose(self, t):
        """-> (QImage with overlays, clean BGR canvas without overlays)"""
        lo, hi = self.current_range(t)
        self.last_range = (lo, hi)
        h, w = t.shape
        OUT_W, OUT_H = w * S, h * S
        CANVAS_W, CANVAS_H = OUT_W + BAR_W, OUT_H
        self.view.sensor_w, self.view.sensor_h = w, h
        canvas = np.zeros((CANVAS_H, CANVAS_W, 3), np.uint8)
        canvas[:, :] = CANVAS
        canvas[:OUT_H, :OUT_W] = self.proc.render(t, lo, hi)
        bar_top, bar_h = 28, OUT_H - 56
        canvas[bar_top:bar_top + bar_h, OUT_W + 8:OUT_W + 26] = self.proc.colorbar(bar_h)
        qimg = QImage(canvas.data, CANVAS_W, CANVAS_H, CANVAS_W * 3, QImage.Format_BGR888).copy()
        f = self.cb_fahrenheit.isChecked()
        u = '°F' if f else '°C'
        fmt = lambda c: f'{c2u(c, f):.1f}'
        p = QPainter(qimg)
        p.setRenderHint(QPainter.Antialiasing)
        # colour bar labels: part of the picture, so also in the "clean" output
        for i in range(6):
            val = hi - (hi - lo) * i / 5
            y = bar_top + bar_h * i / 5
            p.setPen(QColor(220, 220, 220))
            p.drawLine(QPointF(OUT_W + 26, y), QPointF(OUT_W + 31, y))
            outlined_text(p, OUT_W + 33, y + 4, fmt(val), size=9, bold=False)
        outlined_text(p, OUT_W + 6, 18, 'AUTO' if self.rb_auto.isChecked() else 'FIXED',
                      QColor(120, 220, 255) if self.rb_auto.isChecked() else QColor(255, 180, 80), size=9)
        outlined_text(p, OUT_W + 10, OUT_H - 10, u, size=10)
        clean = None
        if (self.recorder or self.rtsp) and not self.cb_overlay_out.isChecked():
            p.end()
            clean = self.qimage_to_bgr(qimg)
            p.begin(qimg)
            p.setRenderHint(QPainter.Antialiasing)
        c = lambda sx: sx * S + S / 2
        if self.cb_hotcold.isChecked():
            iy, ix = np.unravel_index(np.argmax(t), t.shape)
            jy, jx = np.unravel_index(np.argmin(t), t.shape)
            cross(p, c(ix), c(iy), QColor(255, 70, 70), 6)
            cross(p, c(jx), c(jy), QColor(80, 170, 255), 6)
            outlined_text(p, 8, 20, f'max {fmt(t[iy, ix])}{u}', QColor(255, 120, 120), 11)
            outlined_text(p, 8, 40, f'min {fmt(t[jy, jx])}{u}', QColor(130, 190, 255), 11)
        if self.cb_center.isChecked():
            cy, cx = h // 2, w // 2
            v = float(np.median(t[cy - 1:cy + 2, cx - 1:cx + 2]))
            cross(p, c(cx), c(cy), QColor(255, 255, 255), 12)
            outlined_text(p, c(cx) + 10, c(cy) - 10, f'{fmt(v)}{u}')
        for (sx, sy) in self.points:
            cross(p, c(sx), c(sy), QColor(255, 255, 255), 8)
            outlined_text(p, c(sx) + 9, c(sy) - 8, f'{fmt(t[sy, sx])}{u}')
        for (x0, y0, x1, y1) in self.rects:
            r = QRectF(x0 * S, y0 * S, (x1 - x0 + 1) * S, (y1 - y0 + 1) * S)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(0, 0, 0), 3))
            p.drawRect(r)
            p.setPen(QPen(QColor(255, 255, 255), 1.2))
            p.drawRect(r)
            roi = t[y0:y1 + 1, x0:x1 + 1]
            iy, ix = np.unravel_index(np.argmax(roi), roi.shape)
            jy, jx = np.unravel_index(np.argmin(roi), roi.shape)
            cross(p, c(x0 + ix), c(y0 + iy), QColor(255, 70, 70), 5)
            cross(p, c(x0 + jx), c(y0 + jy), QColor(80, 170, 255), 5)
            ty = r.top() - 24 if r.top() > 50 else r.bottom() + 18
            outlined_text(p, r.left(), ty, f'max {fmt(roi.max())}', QColor(255, 130, 130), 10)
            outlined_text(p, r.left(), ty + 17, f'min {fmt(roi.min())}  avg {fmt(roi.mean())}', QColor(140, 200, 255), 10)
        if self.cal_mode:
            outlined_text(p, 10, OUT_H - 14, 'CALIBRATION: click a spot whose temperature you know (Esc cancels)',
                          QColor(0, 255, 255), 11)
        p.end()
        return qimg, clean

    @staticmethod
    def qimage_to_bgr(q):
        q = q.convertToFormat(QImage.Format_BGR888)
        a = np.frombuffer(q.constBits(), np.uint8, q.sizeInBytes()).reshape(q.height(), q.bytesPerLine())
        # always copy: `a` points into q's buffer, which is freed when q goes out of scope
        return a[:, :q.width() * 3].reshape(q.height(), q.width(), 3).copy()

    def update_status(self):
        self.status.setText(f'{self.fps:.1f} fps   |   processing {self.proc_ms:.0f} ms' if self.cam else '')
        red = ui.signal(QApplication.palette()).name()
        chips = [f'<span style="color:{red}">{self.conn_text}</span>' if self.conn_text == 'Not Connected'
                 else self.conn_text]
        tips = [self.conn_tip] if self.conn_tip else []
        if self.recorder:
            chips.append(f'<span style="color:{red}">● REC</span>')
            tips.append('Recording to ' + self.recorder.path)
        if self.rtsp:
            chips.append(f'<span style="color:{red}">● RTSP</span>')
            tips.append('Streaming at ' + self.rtsp.url)
        self.chip.setText('&nbsp;&nbsp;·&nbsp;&nbsp;'.join(chips))
        self.chip.setToolTip(escape('\n'.join(tips)))
        pal = QApplication.palette()
        self.act_rec.setIcon(ui.icon('stop', pal, ui.signal(pal)) if self.recorder else ui.icon('record', pal))

    # ------------------------------------------------------------------ mouse
    def on_click(self, sx, sy):
        if self.cal_mode:
            self.calibration_point(sx, sy)
        else:
            self.points.append((sx, sy))

    def on_right_click(self, sx, sy):
        for i, (px, py) in enumerate(self.points):
            if abs(px - sx) <= 4 and abs(py - sy) <= 4:
                del self.points[i]
                return
        for i in range(len(self.rects) - 1, -1, -1):
            x0, y0, x1, y1 = self.rects[i]
            if x0 <= sx <= x1 and y0 <= sy <= y1:
                del self.rects[i]
                return

    def clear_all(self):
        self.points.clear()
        self.rects.clear()

    # ------------------------------------------------------------------ calibration
    def start_calibration(self):
        if self.temp is None:
            return
        self.cal_mode = True
        self.act_cal_cancel.setEnabled(True)
        self.btn_cal.setText('Click the Image…')

    def cancel_calibration(self):
        self.cal_mode = False
        self.act_cal_cancel.setEnabled(False)
        self.btn_cal.setText('Calibrate…')

    def calibration_point(self, sx, sy):
        self.cancel_calibration()
        d = float(np.median(self.diff[max(sy - 1, 0):sy + 2, max(sx - 1, 0):sx + 2]))
        f = self.cb_fahrenheit.isChecked()
        cur = c2u(self.proc.calib['offset'] + self.proc.calib['gain'] * d, f)
        val, ok = QInputDialog.getDouble(self, 'Temperature calibration',
                                         f'Real temperature at the clicked spot ({"°F" if f else "°C"}):\n'
                                         f'(currently reads {cur:.1f})', cur, -100, 1000, 1)
        if not ok:
            return
        t = u2c(val, f)
        self.cal_points.append((d, t))
        g = self.proc.calib['gain']
        pts = self.cal_points[-2:]
        if len(pts) == 2 and abs(pts[0][0] - pts[1][0]) > 30:
            (d1, t1), (d2, t2) = pts
            g2 = (t1 - t2) / (d1 - d2)
            if 0.001 < g2 < 0.2:
                g = g2
            else:
                QMessageBox.warning(self, 'Calibration', 'The two points are inconsistent; gain not changed.')
        self.proc.set_calib({'gain': g, 'offset': t - g * d})
        self.update_cal_label()

    def reset_calibration(self):
        self.cal_points = []
        self.proc.set_calib(dict(DEFAULT_CALIB))
        self.update_cal_label()

    def update_cal_label(self):
        c = self.proc.calib
        state = 'Default (approximate, ±few °C)' if c == DEFAULT_CALIB else 'User calibrated'
        self.cal_label.setText(f'{state}: gain {c["gain"]:.4f} °C/count, offset {c["offset"]:.2f} °C'
                               + (f', {len(self.cal_points)} point(s) this session' if self.cal_points else ''))

    def lock_range(self):
        f = self.cb_fahrenheit.isChecked()
        lo, hi = self.last_range
        self.rb_fixed.setChecked(True)
        self.sp_min.setValue(c2u(lo, f))
        self.sp_max.setValue(c2u(hi, f))

    def out_size(self):
        """recording / stream frame size = current canvas size (even numbers for H.264)"""
        if self.view.img is not None:
            w, h = self.view.img.width(), self.view.img.height()
        else:
            w, h = IMG_W * S + BAR_W, IMG_H * S
        return w // 2 * 2, h // 2 * 2

    def orientation_widgets_changed(self):
        self.set_orientation(self.cmb_rotation.currentIndex(), self.cb_flip_h.isChecked(), self.cb_flip_v.isChecked())

    def set_orientation(self, rotation, flip_h, flip_v):
        """change rotation / mirroring; cursors and rectangles stay on the same scene spot"""
        old = (self.proc.rotation, self.proc.flip_h, self.proc.flip_v)
        new = (int(rotation) % 4, bool(flip_h), bool(flip_v))
        if new != old:
            def move(x, y):
                sx, sy = unorient_point(x, y, IMG_W, IMG_H, *old)
                return orient_point(sx, sy, IMG_W, IMG_H, *new)
            self.points = [move(x, y) for x, y in self.points]
            rects = []
            for x0, y0, x1, y1 in self.rects:
                ax, ay = move(x0, y0)
                bx, by = move(x1, y1)
                rects.append((min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)))
            self.rects = rects
            self.proc.rotation, self.proc.flip_h, self.proc.flip_v = new
        # keep the widgets in sync when called programmatically
        for w_, fn, val in ((self.cmb_rotation, 'setCurrentIndex', new[0]), (self.cb_flip_h, 'setChecked', new[1]),
                            (self.cb_flip_v, 'setChecked', new[2])):
            w_.blockSignals(True)
            getattr(w_, fn)(val)
            w_.blockSignals(False)

    def rotate(self):
        """next rotation step (90 degrees clockwise, wraps); shortcut R"""
        self.cmb_rotation.setCurrentIndex((self.cmb_rotation.currentIndex() + 1) % 4)

    def units_changed(self, f):
        for sp in (self.sp_min, self.sp_max):
            sp.setValue(c2u(u2c(sp.value(), not f), f))

    # ------------------------------------------------------------------ outputs
    def _export_meta(self):
        meta = {'camera': 'P2 Pro', 'palette': self.proc.palette}
        if self.last_range:
            meta['display_range_C'] = '%.2f..%.2f' % tuple(self.last_range)
        meta['rotation_deg'] = self.proc.rotation * 90
        meta['flip_h'] = self.cb_flip_h.isChecked()
        meta['flip_v'] = self.cb_flip_v.isChecked()
        c = self.proc.calib
        meta['calibration'] = 'gain=%.5f offset=%.3f' % (c['gain'], c['offset'])
        return meta

    def _write_export(self, base, image_ext, data_ext, bgr, temp, fahrenheit, meta):
        """Write image (+ data if data_ext) to base+ext; returns list of written paths."""
        paths = [base + image_ext]
        exporting.save_image(paths[0], bgr)
        if data_ext:
            paths.append(base + data_ext)
            exporting.save_temperature(paths[1], temp, 'F' if fahrenheit else 'C', meta)
        return paths

    def _capture_export(self):
        """Grab image and temperatures at the same moment (frames keep arriving)."""
        if self.temp is None or self.view.img is None:
            return None
        return (self.qimage_to_bgr(self.view.img.copy()), self.temp.copy(),
                self.cb_fahrenheit.isChecked(), self._export_meta())

    def _snap_exts(self):
        return (dict(exporting.IMAGE_FORMATS)[self.cmb_snap_image.currentText()],
                dict(exporting.DATA_FORMATS)[self.cmb_snap_data.currentText()])

    def _report_export(self, paths, f):
        self.statusBar().showMessage('saved ' + ' + '.join(paths) + f' ({"°F" if f else "°C"} per pixel)', 8000)

    def snapshot(self):
        cap = self._capture_export()
        if cap is None:
            return
        d = os.path.join(APP_DIR, 'snapshots')
        image_ext, data_ext = self._snap_exts()
        try:
            os.makedirs(d, exist_ok=True)
            paths = self._write_export(os.path.join(d, time.strftime('thermal_%Y%m%d_%H%M%S')),
                                       image_ext, data_ext, *cap)
        except Exception as e:
            QMessageBox.warning(self, 'Snapshot', str(e))
            return
        self._report_export(paths, cap[2])

    def export_dialog(self):
        """One file dialog for the image; the data file goes next to it with the same base name."""
        cap = self._capture_export()          # before the dialog: frames keep arriving meanwhile
        if cap is None:
            return
        image_ext, data_ext = self._snap_exts()
        name = self.cmb_snap_image.currentText()
        d = os.path.join(APP_DIR, 'snapshots')
        os.makedirs(d, exist_ok=True)
        start = os.path.join(d, time.strftime('thermal_%Y%m%d_%H%M%S') + image_ext)
        path, _ = QFileDialog.getSaveFileName(self, 'Export snapshot', start,
                                              f'{name} (*{image_ext})' + (';;JPEG (*.jpeg)' if image_ext == '.jpg' else ''))
        if not path:
            return
        base, ext = os.path.splitext(path)
        if ext.lower() in exporting.IMAGE_EXTS:
            image_ext = ext
        else:
            base = path
        try:
            paths = self._write_export(base, image_ext, data_ext, *cap)
        except Exception as e:
            QMessageBox.warning(self, 'Export', str(e))
            return
        self._report_export(paths, cap[2])

    def toggle_record(self):
        if self.recorder:
            self.recorder.close()
            self.statusBar().showMessage('saved ' + self.recorder.path, 8000)
            self.recorder = None
            self.act_rec.setText('Start Recording')
            self.act_rec.setIconText('Record')
        else:
            d = os.path.join(APP_DIR, 'recordings')
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, time.strftime('thermal_%Y%m%d_%H%M%S.mp4'))
            try:
                self.recorder = Recorder(path, self.out_size(), FPS, log=print)
                self.act_rec.setText('Stop Recording')
                self.act_rec.setIconText('Stop')
                self.statusBar().showMessage(f'recording {path} ({self.recorder.backend})', 5000)
            except Exception as e:
                QMessageBox.warning(self, 'Recording', str(e))
        self.update_status()

    def toggle_rtsp(self):
        if self.rtsp:
            self.rtsp.close()
            self.rtsp = None
            self.act_rtsp.setText('Start Stream')
            self.act_rtsp.setIconText('Stream')
            self.rtsp_url.clear()
            self.rtsp_url.hide()
            self.rtsp_row_label.hide()
        else:
            try:
                self.rtsp = RtspServer(self.out_size(), FPS, log=print)
                self.act_rtsp.setText('Stop Stream')
                self.act_rtsp.setIconText('Stop')
                self.rtsp_url.setText(self.rtsp.url)
                self.rtsp_url.show()
                self.rtsp_row_label.show()
                self.sections['output'].set_expanded(True)
                self.rtsp_url.setToolTip('Open in VLC: Media > Open Network Stream.\n'
                                         'Windows Firewall may ask to allow mediamtx.exe for other devices to connect.')
            except Exception as e:
                QMessageBox.warning(self, 'RTSP', str(e))
        self.update_status()

    # ------------------------------------------------------------------ settings
    def settings_dict(self):
        s = {'palette': self.cmb_palette.currentText(), 'contrast': self.cmb_contrast.currentText(),
             'upscale': self.cmb_upscale.currentText(), 'fixed': self.rb_fixed.isChecked(),
             'min': self.sp_min.value(), 'max': self.sp_max.value(), 'smooth_range': self.cb_smooth_range.isChecked(),
             'fahrenheit': self.cb_fahrenheit.isChecked(), 'hotcold': self.cb_hotcold.isChecked(),
             'center': self.cb_center.isChecked(), 'rotation': self.cmb_rotation.currentIndex(), 'flip_h': self.cb_flip_h.isChecked(), 'flip_v': self.cb_flip_v.isChecked(),
             'autoshutter': self.sp_autoshutter.value(), 'overlay_out': self.cb_overlay_out.isChecked(),
             'snap_image': self.cmb_snap_image.currentText(), 'snap_data': self.cmb_snap_data.currentText(), 'enh': {}}
        for k, (cb, sl, *_rest) in self.enh.items():
            s['enh'][k] = [cb.isChecked(), sl.value() if sl else None]
        s['sections'] = {k: sec.expanded() for k, sec in self.sections.items()}
        s['inspector'] = self.act_inspector.isChecked()
        if self.scroll.isVisible():
            self.inspector_width = self.scroll.width()
        s['inspector_width'] = self.inspector_width
        s['geometry'] = bytes(self.saveGeometry().toBase64()).decode('ascii')
        return s

    def load_settings(self):
        defaults = {'bad': [True, None], 'stripe': [True, 100], 'denoise': [True, 63], 'dde': [False, 25], 'sharpen': [False, 40]}
        try:
            with open(SETTINGS_FILE) as f:
                s = json.load(f)
        except Exception:
            s = {}
        self.cmb_palette.setCurrentText(s.get('palette', 'Ironbow'))
        self.cmb_contrast.setCurrentText(s.get('contrast', 'Linear'))
        self.cmb_upscale.setCurrentText(s.get('upscale', 'Bicubic'))
        self.proc.palette, self.proc.contrast, self.proc.upscale = (self.cmb_palette.currentText(),
                                                                    self.cmb_contrast.currentText(), self.cmb_upscale.currentText())
        self.cb_fahrenheit.blockSignals(True)      # the stored range is already in this unit
        if s.get('fahrenheit', False):             # segments are exclusive: °C starts checked and unchecks itself
            self.cb_fahrenheit.setChecked(True)
        self.cb_fahrenheit.blockSignals(False)
        (self.rb_fixed if s.get('fixed') else self.rb_auto).setChecked(True)
        self.sp_min.setValue(s.get('min', 20))
        self.sp_max.setValue(s.get('max', 35))
        self.cb_smooth_range.setChecked(s.get('smooth_range', True))
        self.cb_hotcold.setChecked(s.get('hotcold', True))
        self.cb_center.setChecked(s.get('center', False))
        self.set_orientation(int(s.get('rotation', 0)), bool(s.get('flip_h', False)), bool(s.get('flip_v', False)))
        self.sp_autoshutter.setValue(s.get('autoshutter', 60))
        self.cb_overlay_out.setChecked(s.get('overlay_out', True))
        self.cmb_snap_image.setCurrentText(s.get('snap_image', 'PNG'))
        self.cmb_snap_data.setCurrentText(s.get('snap_data', 'CSV'))
        enh = {**defaults, **s.get('enh', {})}
        for k, (cb, sl, attr_on, attr_val, lo, hi) in self.enh.items():
            on, val = enh[k]
            cb.setChecked(on)
            setattr(self.proc, attr_on, on)
            if sl is not None:
                sl.setValue(val if val is not None else 50)
                setattr(self.proc, attr_val, lo + (hi - lo) * sl.value() / 100)
                sl.setEnabled(on)
        for k, on in s.get('sections', {}).items():
            if k in self.sections:
                self.sections[k].set_expanded(on)
        if s.get('geometry'):
            self.restoreGeometry(QByteArray.fromBase64(s['geometry'].encode('ascii')))
        self.inspector_width = int(s.get('inspector_width', 340))
        total = max(sum(self.split.sizes()), self.width())
        self.split.setSizes([total - self.inspector_width, self.inspector_width])
        self.act_inspector.setChecked(bool(s.get('inspector', True)))

    def closeEvent(self, e):
        self.drv_closing.set()
        try:
            with open(SETTINGS_FILE, 'w') as f:
                json.dump(self.settings_dict(), f, indent=2)
        except Exception:
            pass
        self.timer.stop()
        if self.recorder:
            self.recorder.close()
        if self.rtsp:
            self.rtsp.close()
        if self.cam:
            self.cam.close()
        e.accept()


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    app.exec()
    os._exit(0)                     # don't wait for threads blocked in libusb


if __name__ == '__main__':
    main()
