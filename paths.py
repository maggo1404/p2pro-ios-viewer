"""Frozen-aware paths. RES_DIR: read-only bundled resources. APP_DIR: user-writable files."""
import os
import sys

if getattr(sys, 'frozen', False):
    RES_DIR = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    APP_DIR = os.path.dirname(sys.executable)
else:
    RES_DIR = APP_DIR = os.path.dirname(os.path.abspath(__file__))
