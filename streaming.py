"""Video outputs: RTSP server (MediaMTX + ffmpeg) and MP4 recording (ffmpeg, or OpenCV as fallback).

Frames (BGR uint8, fixed size) are piped raw into ffmpeg, which encodes H.264.
ffmpeg is taken from bin/ (bundled in the exe), PATH, or the imageio-ffmpeg pip package, in that order.
Without any ffmpeg, recording falls back to OpenCV's VideoWriter (MPEG-4 Part 2, larger files).
For RTSP, ffmpeg publishes to a local MediaMTX server; clients connect to
rtsp://<this-pc>:8554/thermal (VLC, OBS, ffplay, NVRs, ...).
"""
import glob
import os
import queue
import shutil
import socket
import subprocess
import threading

from paths import RES_DIR, APP_DIR
MEDIAMTX = os.path.join(RES_DIR, 'bin', 'mediamtx.exe')
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def find_ffmpeg():
    bundled = sorted(glob.glob(os.path.join(RES_DIR, 'bin', 'ffmpeg*.exe')))   # put there by p2pro.spec
    if bundled:
        return bundled[-1]
    ff = shutil.which('ffmpeg') or next((p for p in (r'C:\Data\programs\ffmpeg\bin\ffmpeg.exe',) if os.path.exists(p)), None)
    if ff:
        return ff
    try:
        import imageio_ffmpeg                       # pip package that ships an ffmpeg binary
        ff = imageio_ffmpeg.get_ffmpeg_exe()
        return ff if ff and os.path.exists(ff) else None
    except Exception:
        return None


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))           # no packet is sent; just picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        s.close()


class _ThreadedSink:
    """Hands frames to a background writer thread; drops frames if the writer lags."""

    def _start(self):
        self.q = queue.Queue(maxsize=8)
        self.alive = True
        self.done = threading.Event()
        threading.Thread(target=self._writer, daemon=True).start()

    def _writer(self):
        try:
            while self.alive:
                try:
                    f = self.q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if f is None:                       # end marker from _stop(): everything before it is written
                    break
                try:
                    self._write(f)
                except OSError:
                    self.alive = False
        finally:
            self._finish()
            self.done.set()

    def push(self, bgr):
        if not self.alive:
            return
        try:
            self.q.put_nowait(self._prepare(bgr))
        except queue.Full:
            pass

    def _stop(self, timeout=5):
        """Write the frames still queued, then let the writer thread finish."""
        if self.alive:
            try:
                self.q.put(None, timeout=timeout)
            except queue.Full:
                pass
        self.alive = False
        self.done.wait(timeout)


class FFmpegSink(_ThreadedSink):
    """Pipes raw BGR frames into an ffmpeg process."""

    def __init__(self, args, size, fps, log):
        ff = find_ffmpeg()
        if not ff:
            raise RuntimeError('ffmpeg not found (install it or `pip install imageio-ffmpeg`)')
        w, h = size
        cmd = [ff, '-hide_banner', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'bgr24',
               '-s', f'{w}x{h}', '-r', str(fps), '-i', '-'] + args
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        self.log = log
        self._start()
        threading.Thread(target=self._stderr, daemon=True).start()

    def _prepare(self, bgr):
        return bgr.tobytes()

    def _write(self, f):
        self.p.stdin.write(f)

    def _finish(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass

    def _stderr(self):
        for line in self.p.stderr:
            self.log('ffmpeg: ' + line.decode(errors='replace').strip())

    def close(self, wait=True):
        if not wait:
            self.alive = False
            return
        self._stop()                                # writer drains the queue and closes ffmpeg's stdin -> clean EOF
        try:
            self.p.wait(5)
        except subprocess.TimeoutExpired:
            self.p.kill()


class CvSink(_ThreadedSink):
    """Fallback without ffmpeg: OpenCV's VideoWriter, MPEG-4 Part 2 ('mp4v') in an .mp4 file."""

    def __init__(self, path, size, fps):
        import cv2
        self.w = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*'mp4v'), fps, tuple(size))
        if not self.w.isOpened():
            raise RuntimeError(f'OpenCV could not open a video writer for {path}')
        self._start()

    def _prepare(self, bgr):
        return bgr.copy()

    def _write(self, f):
        self.w.write(f)

    def _finish(self):
        self.w.release()

    def close(self):
        self._stop()


class Recorder:
    def __init__(self, path, size, fps, log=print):
        self.path, self.size = path, size
        if find_ffmpeg():
            self.backend = 'ffmpeg H.264'
            self.sink = FFmpegSink(['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-pix_fmt', 'yuv420p',
                                    '-movflags', '+faststart', '-y', path], size, fps, log)
        else:
            self.backend = 'OpenCV MPEG-4'
            self.sink = CvSink(path, size, fps)

    def push(self, bgr):
        self.sink.push(bgr)

    def close(self):
        self.sink.close()


class RtspServer:
    def __init__(self, size, fps, port=8554, path='thermal', log=print):
        if not os.path.exists(MEDIAMTX):
            raise RuntimeError(f'MediaMTX not found at {MEDIAMTX}. Run: python tools/get_mediamtx.py')
        env = dict(os.environ, MTX_RTSPADDRESS=f':{port}', MTX_RTMP='no', MTX_HLS='no', MTX_WEBRTC='no',
                   MTX_SRT='no', MTX_API='no', MTX_METRICS='no', MTX_PPROF='no', MTX_PLAYBACK='no', MTX_LOGLEVEL='info',
                   MTX_LOGDESTINATIONS='file', MTX_LOGFILE=os.path.join(APP_DIR, 'bin', 'mediamtx.log'))
        os.makedirs(os.path.join(APP_DIR, 'bin'), exist_ok=True)
        self.mtx = subprocess.Popen([MEDIAMTX, os.path.join(RES_DIR, 'bin', 'mediamtx.yml')], env=env, cwd=os.path.join(APP_DIR, 'bin'),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        import time
        time.sleep(0.8)
        if self.mtx.poll() is not None:
            raise RuntimeError(f'MediaMTX exited (is port {port} already in use?)')
        self.url = f'rtsp://{lan_ip()}:{port}/{path}'
        self.size = size
        self.sink = FFmpegSink(['-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency', '-g', str(fps),
                                '-pix_fmt', 'yuv420p', '-f', 'rtsp', '-rtsp_transport', 'tcp',
                                f'rtsp://127.0.0.1:{port}/{path}'], size, fps, log)

    def push(self, bgr):
        self.sink.push(bgr)

    @property
    def alive(self):
        return self.sink.alive and self.mtx.poll() is None

    def close(self):
        self.sink.close()                           # let ffmpeg finish before the server goes away
        self.mtx.terminate()
        try:
            self.mtx.wait(3)
        except subprocess.TimeoutExpired:
            self.mtx.kill()
