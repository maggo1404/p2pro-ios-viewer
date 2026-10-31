"""Image processing for the P2 Pro raw stream.

Two stages:
  1. measurement stage (affects temperature readings): shutter correction, bad-pixel repair,
     stripe-noise removal, temporal denoise -> temperature map in C
  2. display stage (only affects the picture): contrast mapping (linear / histogram / CLAHE),
     detail enhancement (DDE), upscaling incl. ESPCN super-resolution, sharpening, palette
"""
import json
import os

import cv2
import numpy as np

from camera import IMG

from paths import RES_DIR, APP_DIR
CALIB_FILE = os.path.join(APP_DIR, 'calib.json')

# Linear model T = offset + gain * (shutter_ref - raw). Defaults estimated from sensor noise
# (~3.6 counts rms at the rated 40 mK NETD); refine with the in-app calibration.
DEFAULT_CALIB = {'gain': 0.012, 'offset': 28.0}


def load_calib():
    try:
        with open(CALIB_FILE) as f:
            c = json.load(f)
        return {'gain': float(c['gain']), 'offset': float(c['offset'])}
    except Exception:
        return dict(DEFAULT_CALIB)


def save_calib(c):
    with open(CALIB_FILE, 'w') as f:
        json.dump(c, f, indent=2)


# ---------------------------------------------------------------- palettes
def _lut(stops):
    """stops: list of (position 0..1, (r, g, b)) -> 256x1x3 BGR LUT"""
    pos = np.array([p for p, _ in stops])
    rgb = np.array([c for _, c in stops], np.float32)
    x = np.linspace(0, 1, 256)
    lut = np.stack([np.interp(x, pos, rgb[:, i]) for i in range(3)], 1)
    return lut[:, ::-1].astype(np.uint8).reshape(256, 1, 3)


PALETTES = {
    'Ironbow': _lut([(0, (0, 0, 10)), (0.15, (40, 0, 110)), (0.35, (140, 0, 150)), (0.55, (220, 60, 40)),
                     (0.75, (250, 150, 0)), (0.9, (255, 220, 60)), (1, (255, 255, 230))]),
    'Inferno': cv2.COLORMAP_INFERNO,
    'White hot': None,
    'Black hot': 'invert',
    'Rainbow': _lut([(0, (0, 0, 90)), (0.2, (0, 60, 255)), (0.4, (0, 220, 220)), (0.6, (60, 230, 0)),
                     (0.8, (255, 200, 0)), (0.92, (255, 40, 0)), (1, (255, 255, 255))]),
    'Turbo': cv2.COLORMAP_TURBO,
    'Jet': cv2.COLORMAP_JET,
    'Lava': _lut([(0, (0, 0, 0)), (0.3, (90, 0, 0)), (0.6, (230, 60, 0)), (0.85, (255, 190, 40)), (1, (255, 255, 255))]),
    'Arctic': _lut([(0, (0, 0, 40)), (0.35, (0, 60, 160)), (0.6, (0, 180, 230)), (0.8, (200, 240, 255)), (1, (255, 255, 255))]),
}


def colorize(gray8, palette):
    p = PALETTES[palette]
    if p is None:
        return cv2.cvtColor(gray8, cv2.COLOR_GRAY2BGR)
    if isinstance(p, str):
        return cv2.cvtColor(255 - gray8, cv2.COLOR_GRAY2BGR)
    if isinstance(p, int):
        return cv2.applyColorMap(gray8, p)
    return cv2.LUT(cv2.cvtColor(gray8, cv2.COLOR_GRAY2BGR), p)


# ---------------------------------------------------------------- super-resolution
class SuperRes:
    """ESPCN (Shi et al. 2016) models from github.com/fannymonori/TF-ESPCN, converted to ONNX
    by models/build_onnx.py because OpenCV can't import their DepthToSpace layer from TF."""

    def __init__(self):
        self.nets = {}

    def available(self, s):
        return os.path.exists(os.path.join(RES_DIR, 'models', f'ESPCN_x{s}.onnx'))

    def __call__(self, gray01, s):
        if s not in self.nets:
            self.nets[s] = cv2.dnn.readNetFromONNX(os.path.join(RES_DIR, 'models', f'ESPCN_x{s}.onnx'))
        net = self.nets[s]
        net.setInput(gray01[None, None].astype(np.float32))
        return net.forward()[0, 0]


# ---------------------------------------------------------------- pipeline
class Processor:
    UPSCALE = ['Nearest', 'Bicubic', 'Lanczos', 'Super-res x2 (ESPCN)', 'Super-res x3 (ESPCN)', 'Super-res x4 (ESPCN)']
    CONTRAST = ['Linear', 'Histogram equalization', 'CLAHE (local contrast)']

    def __init__(self):
        self.calib = load_calib()
        self.sr = SuperRes()
        # measurement stage
        self.bad_pixels = True
        self.destripe = True
        self.destripe_strength = 1.0           # 0..1
        self.denoise = True
        self.denoise_strength = 0.6            # 0..0.95, higher = smoother (more lag on motion)
        # display stage
        self.contrast = 'Linear'
        self.dde = False
        self.dde_strength = 2.0                # detail gain
        self.sharpen = False
        self.sharpen_strength = 0.8
        self.upscale = 'Bicubic'
        self.out_scale = 3                     # output = 256x196 * out_scale
        self.palette = 'Ironbow'
        self.flip_h = self.flip_v = False
        self.rotation = 0                      # quarter turns clockwise (0..3)
        # state
        self._ref_n = -1
        self._bad = None
        self._prev = None

    def reset_temporal(self):
        self._prev = None

    # ---- measurement ----
    def temperature(self, raw, ref, ref_n):
        """raw/ref: full 204x260 frames -> (diff counts, temperature C), both 196x256"""
        if ref_n != self._ref_n:                                   # new shutter reference
            self._ref_n = ref_n
            r = ref[IMG]
            dev = np.abs(r - cv2.medianBlur(r, 5))
            mad = np.median(dev) + 1e-3
            self._bad = dev > 12 * mad                             # stuck / hot pixels in the flat field
            self._prev = None
        d = (ref - raw)[IMG]
        if self.bad_pixels:
            med = cv2.medianBlur(d, 3)
            d = np.where(self._bad, med, d)
            # also catch blinking pixels: replace strong isolated outliers
            dev = np.abs(d - med)
            d = np.where(dev > 60, med, d)
        if self.destripe and self.destripe_strength > 0:
            hp = d - cv2.blur(d, (9, 9))                           # high-pass
            col = np.median(hp, 0, keepdims=True)
            row = np.median(hp, 1, keepdims=True)
            d = d - self.destripe_strength * (col + row)
        if self.denoise and self._prev is not None:
            a = self.denoise_strength
            motion = np.abs(d - self._prev)
            w = a * np.exp(-motion / 25.0)                         # keep less history where things move
            d = w * self._prev + (1 - w) * d
        self._prev = d
        t = self.calib['offset'] + self.calib['gain'] * d
        if self.flip_h:
            d, t = d[:, ::-1], t[:, ::-1]
        if self.flip_v:
            d, t = d[::-1], t[::-1]
        if self.rotation:
            d, t = np.rot90(d, -self.rotation), np.rot90(t, -self.rotation)
        return np.ascontiguousarray(d), np.ascontiguousarray(t)

    # ---- display ----
    def render(self, t, lo, hi):
        """temperature map -> BGR image (h*out_scale, w*out_scale)"""
        n = np.clip((t - lo) / max(hi - lo, 1e-3), 0, 1).astype(np.float32)
        if self.dde:
            base = cv2.bilateralFilter(n, 0, 0.08, 3)
            detail = n - base
            n = np.clip(base + self.dde_strength * detail, 0, 1)
        if self.contrast == 'Histogram equalization':
            g = (n * 255).astype(np.uint8)
            hist = np.bincount(g.ravel(), minlength=256).astype(np.float64)
            hist = np.minimum(hist, hist.mean() * 4)               # plateau HE: avoid over-stretching flat areas
            cdf = hist.cumsum()
            cdf = (cdf - cdf[0]) / max(cdf[-1] - cdf[0], 1)
            n = (0.7 * cdf[g] + 0.3 * n).astype(np.float32)
        elif self.contrast == 'CLAHE (local contrast)':
            g = (n * 65535).astype(np.uint16)
            n = cv2.createCLAHE(2.5, (6, 6)).apply(g).astype(np.float32) / 65535
        h, w = n.shape
        s = self.out_scale
        size = (w * s, h * s)
        if self.upscale.startswith('Super-res'):
            k = int(self.upscale.split('x')[1][0])
            big = np.clip(self.sr(n, k), 0, 1)
            if k != s:
                big = cv2.resize(big, size, interpolation=cv2.INTER_CUBIC if k < s else cv2.INTER_AREA)
        else:
            interp = {'Nearest': cv2.INTER_NEAREST, 'Bicubic': cv2.INTER_CUBIC, 'Lanczos': cv2.INTER_LANCZOS4}[self.upscale]
            big = cv2.resize(n, size, interpolation=interp)
        if self.sharpen:
            blur = cv2.GaussianBlur(big, (0, 0), 1.2 * s / 2)
            big = np.clip(big + self.sharpen_strength * (big - blur), 0, 1)
        return colorize((np.clip(big, 0, 1) * 255).astype(np.uint8), self.palette)

    def colorbar(self, h, w=18):
        ramp = np.linspace(255, 0, h).astype(np.uint8)[:, None].repeat(w, 1)
        return colorize(ramp, self.palette)

    # ---- calibration ----
    def set_calib(self, c):
        self.calib = c
        save_calib(c)
