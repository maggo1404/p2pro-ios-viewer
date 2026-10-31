# PyInstaller spec: pyinstaller p2pro.spec
import glob, os
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

datas = [('init_cmds.json', '.'), ('bin/mediamtx.yml', 'bin')]
datas += [(f, 'models') for f in glob.glob('models/*.onnx')]
binaries = []
if os.path.exists('bin/mediamtx.exe'):
    datas.append(('bin/mediamtx.exe', 'bin'))
else:
    print('WARNING: bin/mediamtx.exe missing; run tools/get_mediamtx.py (RTSP streaming will not work)')

datas += collect_data_files('libusb_package')
binaries += collect_dynamic_libs('libusb_package')

a = Analysis(
    ['app.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=['usb.backend.libusb1', 'libusb_package'],
    excludes=['tkinter', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtQml',
              'PySide6.QtQuick', 'PySide6.Qt3DCore', 'PySide6.QtCharts', 'PySide6.QtDataVisualization'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='P2ProViewer',
    console=False,
    upx=False,
    runtime_tmpdir=None,
)
