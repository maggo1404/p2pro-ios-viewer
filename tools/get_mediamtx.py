"""Download the MediaMTX RTSP server (github.com/bluenviron/mediamtx, MIT) into bin/.
Needed only for the RTSP stream feature of app.py."""
import io
import json
import os
import urllib.request
import zipfile

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'bin')

req = urllib.request.Request('https://api.github.com/repos/bluenviron/mediamtx/releases/latest')
if os.environ.get('GITHUB_TOKEN'):
    req.add_header('Authorization', 'Bearer ' + os.environ['GITHUB_TOKEN'])
rel = json.load(urllib.request.urlopen(req))
asset = next(a for a in rel['assets'] if a['name'].endswith('windows_amd64.zip'))
print('downloading', asset['name'])
data = urllib.request.urlopen(asset['browser_download_url']).read()
with zipfile.ZipFile(io.BytesIO(data)) as z:
    z.extract('mediamtx.exe', BIN)
    if not os.path.exists(os.path.join(BIN, 'mediamtx.yml')):
        z.extract('mediamtx.yml', BIN)
print('installed', os.path.join(BIN, 'mediamtx.exe'))
