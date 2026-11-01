"""P2 Pro (Lightning) thermal camera app - Qt GUI with side panel.

Run:  python app.py        (unplug + replug the camera before every start)

Mouse on the image:
  left click   add a temperature cursor
  left drag    draw a rectangle (max / min / average inside)
  right click  delete the cursor / rectangle under the mouse
"""
import json
import os
import threading
import time

import cv2
import numpy as np
from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QImage, QKeySequence, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox,
                               QPushButton, QRadioButton, QScrollArea, QSlider, QSpinBox, QVBoxLayout, QWidget)

import exporting
from camera import IMG_H, IMG_W, Camera, CameraError
from processing import DEFAULT_CALIB, PALETTES, Processor, orient_point, unorient_point
from streaming import Recorder, RtspServer, find_ffmpeg

from paths import APP_DIR
SETTINGS_FILE = os.path.join(APP_DIR, 'settings.json')
S = 3                                   # output scale: 256x196 -> 768x588 (196x256 when rotated)
BAR_W = 72                              # colour bar area on the right of the output image
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
        _FONTS[key] = QFont('Segoe UI', size, QFont.Bold if bold else QFont.Normal)
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
        self.message = 'Connecting...'
        self.sensor_w, self.sensor_h = IMG_W, IMG_H   # size of the displayed (possibly rotated) image

    def set_image(self, qimg):
        self.img = qimg
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(24, 24, 24))
        if self.img is None:
            p.setPen(QColor(200, 200, 200))
            p.setFont(QFont('Segoe UI', 12))
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


# =====================================================================================
class MainWindow(QMainWindow):
    log_signal = Signal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle('P2 Pro thermal (Lightning)')
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

        self.view = ImageView()
        self.view.clicked.connect(self.on_click)
        self.view.rect_drawn.connect(lambda *r: self.rects.append(r))
        self.view.right_clicked.connect(self.on_right_click)

        panel = self.build_panel()
        scroll = QScrollArea()
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(330)
        central = QWidget()
        lay = QHBoxLayout(central)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(self.view, 1)
        lay.addWidget(scroll)
        self.setCentralWidget(central)
        self.status = QLabel()
        self.statusBar().addWidget(self.status, 1)
        self.log_signal.connect(self.on_log)
        self.add_shortcuts()
        self.load_settings()
        self.resize(1200, 700)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(5)
        QTimer.singleShot(200, self.connect_camera)

    # ------------------------------------------------------------------ panel
    def build_panel(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(8)

        # --- camera
        g = QGroupBox('Camera')
        f = QVBoxLayout(g)
        self.cam_label = QLabel('not connected')
        self.cam_label.setWordWrap(True)
        self.btn_connect = QPushButton('Connect (replug camera first)')
        self.btn_connect.clicked.connect(self.connect_camera)
        self.btn_shutter = QPushButton('Shutter calibration (flat field)')
        self.btn_shutter.clicked.connect(self.shutter)
        row = QHBoxLayout()
        row.addWidget(QLabel('Auto shutter every'))
        self.sp_autoshutter = QSpinBox()
        self.sp_autoshutter.setRange(0, 600)
        self.sp_autoshutter.setValue(60)
        self.sp_autoshutter.setSuffix(' s')
        self.sp_autoshutter.setSpecialValueText('off')
        row.addWidget(self.sp_autoshutter)
        f.addWidget(self.cam_label)
        f.addWidget(self.btn_connect)
        f.addWidget(self.btn_shutter)
        f.addLayout(row)
        v.addWidget(g)

        # --- measurement
        g = QGroupBox('Measurement')
        f = QVBoxLayout(g)
        hint = QLabel('Click image: cursor · drag: rectangle · right click: delete')
        hint.setWordWrap(True)
        hint.setStyleSheet('color: gray')
        f.addWidget(hint)
        row = QHBoxLayout()
        for text, fn in (('Clear cursors', lambda: self.points.clear()), ('Clear rects', lambda: self.rects.clear()),
                         ('Clear all', self.clear_all)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        f.addLayout(row)
        self.cb_hotcold = QCheckBox('Mark hottest / coldest spot')
        self.cb_hotcold.setChecked(True)
        self.cb_center = QCheckBox('Center crosshair')
        self.cb_fahrenheit = QCheckBox('Fahrenheit')
        for cb in (self.cb_hotcold, self.cb_center, self.cb_fahrenheit):
            f.addWidget(cb)
        self.cb_fahrenheit.toggled.connect(self.units_changed)
        row = QHBoxLayout()
        self.btn_cal = QPushButton('Calibrate temp...')
        self.btn_cal.setToolTip('Click a spot whose real temperature you know, then enter it.\n'
                                'One point fixes the offset, two points (different temperatures) also fix the gain.')
        self.btn_cal.clicked.connect(self.start_calibration)
        b = QPushButton('Reset calibration')
        b.clicked.connect(self.reset_calibration)
        row.addWidget(self.btn_cal)
        row.addWidget(b)
        f.addLayout(row)
        self.cal_label = QLabel()
        self.cal_label.setWordWrap(True)
        self.cal_label.setStyleSheet('color: gray')
        f.addWidget(self.cal_label)
        v.addWidget(g)

        # --- display
        g = QGroupBox('Display')
        f = QFormLayout(g)
        self.cmb_palette = QComboBox()
        self.cmb_palette.addItems(list(PALETTES))
        self.cmb_palette.currentTextChanged.connect(lambda t: setattr(self.proc, 'palette', t))
        f.addRow('Palette', self.cmb_palette)
        row = QHBoxLayout()
        self.rb_auto = QRadioButton('Auto')
        self.rb_fixed = QRadioButton('Fixed')
        self.rb_auto.setChecked(True)
        grp = QButtonGroup(g)
        grp.addButton(self.rb_auto)
        grp.addButton(self.rb_fixed)
        row.addWidget(self.rb_auto)
        row.addWidget(self.rb_fixed)
        b = QPushButton('Lock current')
        b.setToolTip('Switch to fixed scale using the current auto range')
        b.clicked.connect(self.lock_range)
        row.addWidget(b)
        f.addRow('Scale', row)
        self.sp_min, self.sp_max = QDoubleSpinBox(), QDoubleSpinBox()
        for sp, val in ((self.sp_min, 20), (self.sp_max, 35)):
            sp.setRange(-100, 1000)
            sp.setDecimals(1)
            sp.setSingleStep(0.5)
            sp.setValue(val)
        row = QHBoxLayout()
        row.addWidget(self.sp_min)
        row.addWidget(QLabel('to'))
        row.addWidget(self.sp_max)
        f.addRow('Range', row)
        self.cb_smooth_range = QCheckBox('smooth auto range (less flicker)')
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
        self.cmb_rotation.setToolTip('Rotate the image clockwise (shortcut R = next step)')
        self.cmb_rotation.currentIndexChanged.connect(lambda _i: self.orientation_widgets_changed())
        f.addRow('Rotation', self.cmb_rotation)
        row = QHBoxLayout()
        self.cb_flip_h, self.cb_flip_v = QCheckBox('Mirror horizontal'), QCheckBox('Mirror vertical')
        self.cb_flip_h.setToolTip('Mirror left/right on screen (applied after rotation)')
        self.cb_flip_v.setToolTip('Mirror top/bottom on screen (applied after rotation)')
        self.cb_flip_h.toggled.connect(lambda _b: self.orientation_widgets_changed())
        self.cb_flip_v.toggled.connect(lambda _b: self.orientation_widgets_changed())
        row.addWidget(self.cb_flip_h)
        row.addWidget(self.cb_flip_v)
        f.addRow('Mirror', row)
        v.addWidget(g)

        # --- enhancement
        g = QGroupBox('Image enhancement')
        f = QFormLayout(g)
        note = QLabel('Denoise/stripe/bad-pixel also clean the temperature readings; '
                      'the others only change the picture.')
        note.setWordWrap(True)
        note.setStyleSheet('color: gray')
        f.addRow(note)
        self.enh = {}

        def enh_row(key, label, attr_on, attr_val=None, lo=0, hi=1, tip=''):
            cb = QCheckBox(label)
            cb.setToolTip(tip)
            cb.toggled.connect(lambda b: setattr(self.proc, attr_on, b))
            sl = None
            if attr_val:
                sl = QSlider(Qt.Horizontal)
                sl.setRange(0, 100)
                sl.setToolTip('strength')
                sl.valueChanged.connect(lambda x: setattr(self.proc, attr_val, lo + (hi - lo) * x / 100))
            f.addRow(cb, sl) if sl else f.addRow(cb)
            self.enh[key] = (cb, sl, attr_on, attr_val, lo, hi)
        enh_row('bad', 'Bad pixel repair', 'bad_pixels', tip='Replace stuck/blinking pixels with their neighbours')
        enh_row('stripe', 'Stripe removal', 'destripe', 'destripe_strength', 0, 1,
                'Remove row/column fixed-pattern noise (vertical/horizontal lines)')
        enh_row('denoise', 'Temporal denoise', 'denoise', 'denoise_strength', 0, 0.95,
                'Average over time where the scene is still (motion adaptive)')
        enh_row('dde', 'Detail enhancement (DDE)', 'dde', 'dde_strength', 1, 5,
                'Digital detail enhancement: boost fine detail while compressing the big temperature range')
        enh_row('sharpen', 'Sharpen', 'sharpen', 'sharpen_strength', 0, 2, 'Unsharp mask after upscaling')
        v.addWidget(g)

        # --- output
        g = QGroupBox('Output')
        f = QVBoxLayout(g)
        row = QHBoxLayout()
        b = QPushButton('Snapshot')
        b.setToolTip('Quick save (S): screenshot + per-pixel temperature data, auto-named in the snapshots folder, '
                     'in the formats chosen below')
        b.clicked.connect(self.snapshot)
        b_exp = QPushButton('Export…')
        b_exp.setToolTip('Choose the file name and folder; the temperature data is saved next to the image '
                         'with the same name')
        b_exp.clicked.connect(self.export_dialog)
        self.btn_rec = QPushButton('Start recording')
        self.btn_rec.clicked.connect(self.toggle_record)
        row.addWidget(b)
        row.addWidget(b_exp)
        f.addLayout(row)
        f.addWidget(self.btn_rec)
        row = QHBoxLayout()
        self.cmb_snap_image = QComboBox()
        self.cmb_snap_image.addItems([n for n, _ in exporting.IMAGE_FORMATS])
        self.cmb_snap_image.setToolTip('Screenshot format (includes overlays and colour bar)')
        self.cmb_snap_data = QComboBox()
        self.cmb_snap_data.addItems([n for n, _ in exporting.DATA_FORMATS])
        self.cmb_snap_data.setToolTip('Per-pixel temperatures (in the selected unit, same pixel grid as the '
                                      'unscaled image): CSV / XML text, or float32 TIFF for ImageJ / Python')
        row.addWidget(QLabel('Image'))
        row.addWidget(self.cmb_snap_image)
        row.addWidget(QLabel('Data'))
        row.addWidget(self.cmb_snap_data)
        f.addLayout(row)
        self.btn_rtsp = QPushButton('Start RTSP stream')
        self.btn_rtsp.clicked.connect(self.toggle_rtsp)
        f.addWidget(self.btn_rtsp)
        self.rtsp_url = QLineEdit()
        self.rtsp_url.setReadOnly(True)
        self.rtsp_url.setPlaceholderText('stream URL appears here')
        f.addWidget(self.rtsp_url)
        self.cb_overlay_out = QCheckBox('Include cursors / labels in recording and stream')
        self.cb_overlay_out.setChecked(True)
        f.addWidget(self.cb_overlay_out)
        if not find_ffmpeg():             # recording falls back to OpenCV; RTSP needs ffmpeg
            self.btn_rtsp.setEnabled(False)
            self.btn_rtsp.setToolTip('ffmpeg not found')
            self.btn_rec.setToolTip('ffmpeg not found: recording uses OpenCV (MPEG-4, larger files)')
        v.addWidget(g)
        v.addStretch(1)
        self.update_cal_label()
        return w

    def add_shortcuts(self):
        for key, fn in (('Delete', self.clear_all), ('S', self.snapshot), ('C', self.shutter), ('R', self.rotate),
                        ('A', lambda: (self.rb_fixed if self.rb_auto.isChecked() else self.rb_auto).setChecked(True)),
                        ('Escape', self.cancel_calibration)):
            a = QAction(self)
            a.setShortcut(QKeySequence(key))
            a.triggered.connect(fn)
            self.addAction(a)

    # ------------------------------------------------------------------ camera
    def connect_camera(self):
        if self.connecting:
            return
        if self.cam:
            self.cam.close()
            self.cam = None
        self.connecting = True
        self.btn_connect.setEnabled(False)
        self.cam_label.setText('connecting... (takes ~6 s)')
        self.view.img = None
        self.view.message = 'Connecting to camera...'
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
        if s == '@connected':
            self.connecting = False
            self.btn_connect.setEnabled(True)
            self.cam_label.setText('connected, streaming')
            self.last_shutter = time.time()
            self.proc.reset_temporal()
        elif s.startswith('@failed'):
            self.connecting = False
            self.btn_connect.setEnabled(True)
            self.cam_label.setText('<b>not connected</b>: ' + s[8:])
            self.view.message = 'No camera.\n\n' + s[8:] + '\n\nUnplug the camera, plug it back in, then press Connect.'
            self.view.update()
        else:
            print(s)

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
        canvas[:, :] = (30, 30, 30)
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
        parts = [f'{self.fps:.1f} fps', f'processing {self.proc_ms:.0f} ms']
        if self.recorder:
            parts.append('● REC ' + os.path.basename(self.recorder.path))
        if self.rtsp:
            parts.append('● RTSP ' + self.rtsp.url)
        self.status.setText('   |   '.join(parts))

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
        self.btn_cal.setText('Click the image...')

    def cancel_calibration(self):
        self.cal_mode = False
        self.btn_cal.setText('Calibrate temp...')

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
        state = 'default (approximate, ±few °C)' if c == DEFAULT_CALIB else 'user calibrated'
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
            self.btn_rec.setText('Start recording')
        else:
            d = os.path.join(APP_DIR, 'recordings')
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, time.strftime('thermal_%Y%m%d_%H%M%S.mp4'))
            try:
                self.recorder = Recorder(path, self.out_size(), FPS, log=print)
                self.btn_rec.setText('■ Stop recording')
                self.statusBar().showMessage(f'recording {path} ({self.recorder.backend})', 5000)
            except Exception as e:
                QMessageBox.warning(self, 'Recording', str(e))
        self.update_status()

    def toggle_rtsp(self):
        if self.rtsp:
            self.rtsp.close()
            self.rtsp = None
            self.btn_rtsp.setText('Start RTSP stream')
            self.rtsp_url.clear()
        else:
            try:
                self.rtsp = RtspServer(self.out_size(), FPS, log=print)
                self.btn_rtsp.setText('■ Stop RTSP stream')
                self.rtsp_url.setText(self.rtsp.url)
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
        self.cb_fahrenheit.blockSignals(True)
        self.cb_fahrenheit.setChecked(s.get('fahrenheit', False))
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

    def closeEvent(self, e):
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
    import sys
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    app.exec()
    os._exit(0)                     # don't wait for threads blocked in libusb


if __name__ == '__main__':
    main()
