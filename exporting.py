"""Export helpers: screenshots and per-pixel temperature data (no Qt dependency)."""
import os
import time
from xml.sax.saxutils import escape, quoteattr

import cv2
import numpy as np

# (label shown in the UI, file extension)
IMAGE_FORMATS = [('PNG', '.png'), ('JPEG', '.jpg')]
DATA_FORMATS = [('CSV', '.csv'), ('XML', '.xml'), ('TIFF float32', '.tif'), ('None', None)]
IMAGE_EXTS = ('.png', '.jpg', '.jpeg')
DATA_EXTS = ('.csv', '.xml', '.tif', '.tiff')


def _clean(v):
    return ' '.join(str(v).split())            # single line, no control chars


def _meta_items(meta):
    return [(str(k), _clean(v)) for k, v in (meta or {}).items()]


def save_temperature(path, temp_c, unit='C', meta=None):
    """Write a 2-D temperature array. unit 'C' or 'F' converts the values; the format follows the extension.

    temp_c is always in degrees Celsius. TIFF is always written in the chosen unit as float32.
    """
    path = os.fspath(path)
    ext = os.path.splitext(path)[1].lower()
    if ext not in DATA_EXTS:
        raise ValueError(f'unsupported data format {ext!r} (use {", ".join(DATA_EXTS)})')
    unit = unit.upper().lstrip('°')
    if unit not in ('C', 'F'):
        raise ValueError(f'unknown unit {unit!r}')
    t = np.asarray(temp_c, np.float64)
    if t.ndim != 2:
        raise ValueError('temperature array must be 2-D')
    if unit == 'F':
        t = t * 9 / 5 + 32
    h, w = t.shape
    stamp = time.strftime('%Y-%m-%dT%H:%M:%S')
    items = [('timestamp', stamp), ('width', w), ('height', h), ('unit', unit)] + _meta_items(meta)
    items = [(k, _clean(v)) for k, v in items]
    if ext == '.csv':
        header = '\n'.join(f'# {k}: {v}' for k, v in items) + \
            '\n# layout: one line per image row (top to bottom), one value per pixel (left to right)'
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(header + '\n')
            np.savetxt(f, t, fmt='%.2f', delimiter=',')
    elif ext == '.xml':
        d = dict(items)
        out = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<!-- Thermal image. <data> holds one <row y="N"> per image row (y=0 is the top row);',
               '     each row has `width` space-separated temperatures in the given unit, the i-th value is the',
               '     pixel at x=i (x=0 is the left column). -->',
               f'<thermalImage width="{w}" height="{h}" unit={quoteattr(d["unit"])} timestamp={quoteattr(stamp)}>',
               '  <meta>']
        for k, v in items:
            tag = ''.join(c if c.isalnum() or c in '_-' else '_' for c in k) or 'key'
            if not (tag[0].isalpha() or tag[0] == '_'):
                tag = '_' + tag
            out.append(f'    <{tag}>{escape(v)}</{tag}>')
        out.append('  </meta>')
        out.append('  <data>')
        rows = np.char.mod('%.2f', t)
        for y in range(h):
            out.append(f'    <row y="{y}">{" ".join(rows[y])}</row>')
        out += ['  </data>', '</thermalImage>', '']
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write('\n'.join(out))
    else:                                       # TIFF
        _write_image(path, np.ascontiguousarray(t, np.float32))


def _write_image(path, arr, params=()):
    ext = os.path.splitext(path)[1]
    ok, buf = cv2.imencode(ext, arr, list(params))
    if not ok:
        raise IOError(f'could not encode {path}')
    buf.tofile(path)                            # works with non-ASCII paths on Windows


def save_image(path, bgr, jpeg_quality=95):
    """Write a BGR uint8 image as .png / .jpg / .jpeg."""
    path = os.fspath(path)
    ext = os.path.splitext(path)[1].lower()
    if ext not in IMAGE_EXTS:
        raise ValueError(f'unsupported image format {ext!r} (use {", ".join(IMAGE_EXTS)})')
    params = [cv2.IMWRITE_JPEG_QUALITY, int(jpeg_quality)] if ext != '.png' else []
    _write_image(path, np.ascontiguousarray(bgr), params)
