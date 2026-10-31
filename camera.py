"""P2 Pro (Lightning / iOS version) camera driver.

Plays the iPhone's role: iAP2 handshake (iap2.py), open the External Accessory data channel,
send the iOS app's init sequence (init_cmds.json) and decode the raw sensor stream.
"""
import json
import os
import queue
import threading
import time

import numpy as np

from iap2 import Link, msg, usb

from paths import RES_DIR
INIT_CMDS = json.load(open(os.path.join(RES_DIR, 'init_cmds.json')))

FRAME_BYTES = 106080                  # 260 x 204 x uint16 (210 USB packets x 506 payload bytes)
FRAME_SHAPE = (204, 260)
IMG = (slice(6, 202), slice(2, 258))  # 256 x 196 image; other rows/cols are header/reference/telemetry
IMG_H, IMG_W = 196, 256

SHUTTER_CLOSE = ('06 1e 00 10 00 10', '06 1e 00 10 00 00')
SHUTTER_OPEN = ('06 1e 00 40 00 40', '06 1e 00 40 00 00')


class CameraError(Exception):
    pass



class Camera:
    """Connect with Camera(); frames arrive in a background thread.
    Use wait_frame() / latest() to get raw frames (float32, 204 x 260)."""

    def __init__(self, log=print):
        self.log = log
        self.l = None
        try:
            self.l = l = Link()
            l.connect()
        except SystemExit as e:
            self._release()
            raise CameraError('camera did not answer the iAP2 handshake - unplug it, plug it back in, retry') from e
        except usb.core.NoBackendError as e:
            raise CameraError('libusb backend not found (pip install libusb-package)') from e
        except (AttributeError, usb.core.USBError) as e:
            raise CameraError(f'camera not found or busy ({e}) - is it plugged in and is Thermal Master closed?') from e
        c = l.sess

        def x(mid, params=(), wait=3000):
            l.send(c, msg(mid, params))
            return l.recv(wait)
        x(0xAA00)                                   # RequestAuthenticationCertificate
        x(0xAA02, [(0, os.urandom(32))])            # RequestAuthenticationChallengeResponse
        l.send(c, msg(0xAA05))                      # AuthenticationSucceeded (the host side doesn't verify)
        x(0x1D00)                                   # StartIdentification
        l.send(c, msg(0x1D02))                      # IdentificationAccepted
        l.recv(1500)                                # camera sends RequestAppLaunch; ignore
        self.d = d = l.d
        usb.util.claim_interface(d, 1)
        d.set_interface_altsetting(1, 1)            # open the EA native-transport data channel
        log('iAP2 handshake ok')

        self.run = True
        self.q = queue.Queue()
        self.cv = threading.Condition()
        self.frame, self.nframe = None, 0
        self.calibrating = False
        self.ref = None                             # closed-shutter reference frame
        self.ref_n = 0                              # increments on every new reference
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._keep_link, daemon=True).start()

        t0 = time.time()
        for cmd in INIT_CMDS:                       # send the iOS app's init sequence, ends with start-stream
            self.cmd(cmd)
            try:
                self.q.get(timeout=0.5)
            except queue.Empty:
                pass
        log(f'init sequence sent ({len(INIT_CMDS)} commands, {time.time() - t0:.1f}s)')
        threading.Thread(target=self._frame_loop, daemon=True).start()

    # ---- low level ----
    def cmd(self, hexstr):
        self.d.write(0x07, bytes.fromhex(hexstr), 1000)

    def pulse(self, pair):
        self.cmd(pair[0])
        time.sleep(0.07)
        self.cmd(pair[1])

    def _reader(self):
        while self.run:
            try:
                self.q.put(bytes(self.d.read(0x82, 1 << 20, 500)))
            except usb.core.USBTimeoutError:
                pass
            except Exception as e:
                if self.run:
                    self.log(f'USB read error: {e}')
                self.run = False
                with self.cv:
                    self.cv.notify_all()

    def _keep_link(self):                           # keep ACKing the iAP2 control link
        while self.run:
            try:
                self.l.recv(200)
            except Exception:
                time.sleep(0.2)

    def _frame_loop(self):
        cur, fid = bytearray(), None
        while self.run:
            try:
                chunk = self.q.get(timeout=1)
            except queue.Empty:
                continue
            for o in range(0, len(chunk), 512):
                p = chunk[o:o + 512]
                if len(p) < 8 or p[0] != 6 or not (p[1] & 0x80):
                    continue                        # command replies / junk
                f = p[1] & 1                        # frame-id bit toggles every frame
                if fid is not None and f != fid:
                    if len(cur) == FRAME_BYTES:
                        fr = np.frombuffer(bytes(cur), '<u2').reshape(FRAME_SHAPE).astype(np.float32)
                        with self.cv:
                            self.frame, self.nframe = fr, self.nframe + 1
                            self.cv.notify_all()
                    cur = bytearray()
                fid = f
                cur += p[6:]

    # ---- public ----
    def wait_frame(self, after, timeout=2.0):
        with self.cv:
            self.cv.wait_for(lambda: self.nframe > after or not self.run, timeout)
            return self.frame, self.nframe

    def shutter_calibrate(self):
        """Close the shutter, average frames as flat-field reference, reopen. Blocks ~1 s."""
        self.calibrating = True
        try:
            self.pulse(SHUTTER_CLOSE)
            time.sleep(0.35)
            _, n = self.wait_frame(0, 0)
            refs = []
            while len(refs) < 8 and self.run:
                fr, n = self.wait_frame(n)
                if fr is not None:
                    refs.append(fr)
            self.pulse(SHUTTER_OPEN)
            time.sleep(0.25)
            if refs:
                self.ref = np.mean(refs, 0)
                self.ref_n += 1
        finally:
            self.calibrating = False

    def _release(self):
        if self.l is None:
            return
        self.l.run = False
        time.sleep(0.3)                             # let the link reader thread exit
        try:
            usb.util.dispose_resources(self.l.d)
        except Exception:
            pass

    def close(self):
        if getattr(self, 'run', False):
            try:
                self.cmd('03 02 00')                # stop stream
            except Exception:
                pass
        self.run = False
        self._release()
