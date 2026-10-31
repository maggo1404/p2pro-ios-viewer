"""Video outputs: RTSP server (MediaMTX + ffmpeg) and MP4 recording (ffmpeg).

Frames (BGR uint8, fixed size) are piped raw into ffmpeg, which encodes H.264.
For RTSP, ffmpeg publishes to a local MediaMTX server; clients connect to
rtsp://<this-pc>:8554/thermal (VLC, OBS, ffplay, NVRs, ...).
"""
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
    return shutil.which('ffmpeg') or next((p for p in (r'C:\Data\programs\ffmpeg\bin\ffmpeg.exe',) if os.path.exists(p)), None)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))           # no packet is sent; just picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        s.close()


class FFmpegSink:
    """Feeds frames to an ffmpeg process in a background thread; drops frames if ffmpeg lags."""

    def __init__(self, args, size, fps, log):
        ff = find_ffmpeg()
        if not ff:
            raise RuntimeError('ffmpeg not found on PATH')
        w, h = size
        cmd = [ff, '-hide_banner', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'bgr24',
               '-s', f'{w}x{h}', '-r', str(fps), '-i', '-'] + args
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        self.q = queue.Queue(maxsize=8)
        self.log = log
        self.alive = True
        threading.Thread(target=self._writer, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()

    def _writer(self):
        while self.alive:
            try:
                f = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if f is None:
                break
            try:
                self.p.stdin.write(f)
            except OSError:
                self.alive = False
        try:
            self.p.stdin.close()
        except OSError:
            pass

    def _stderr(self):
        for line in self.p.stderr:
            self.log('ffmpeg: ' + line.decode(errors='replace').strip())

    def push(self, bgr):
        if not self.alive:
            return
        try:
            self.q.put_nowait(bgr.tobytes())
        except queue.Full:
            pass

    def close(self, wait=True):
        self.alive = False                          # writer thread exits and closes ffmpeg's stdin -> clean EOF
        if wait:
            try:
                self.p.wait(5)
            except subprocess.TimeoutExpired:
                self.p.kill()


class Recorder:
    def __init__(self, path, size, fps, log=print):
        self.path, self.size = path, size
        self.sink = FFmpegSink(['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-pix_fmt', 'yuv420p',
                                '-movflags', '+faststart', '-y', path], size, fps, log)

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
