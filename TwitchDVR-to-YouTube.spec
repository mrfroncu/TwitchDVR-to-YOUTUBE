# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: single-file windowed executable.

Build with:  pyinstaller TwitchDVR-to-YouTube.spec --noconfirm
Output:      dist/TwitchDVR-to-YouTube.exe   (Windows)
             dist/TwitchDVR-to-YouTube.app   (macOS)
"""
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

spec_dir = os.path.dirname(os.path.abspath(SPEC))
sys.path.insert(0, spec_dir)
from app.version import __version__  # noqa: E402

# googleapiclient needs its bundled API discovery documents and the package
# metadata of the google libs at runtime.
datas = collect_data_files("googleapiclient.discovery_cache")
datas += collect_data_files("sv_ttk")   # theme .tcl files
for pkg in ("google-api-python-client", "google-auth", "google-auth-oauthlib",
            "google-auth-httplib2"):
    datas += copy_metadata(pkg)

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=["pywinstyles"] if sys.platform == "win32" else [],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="TwitchDVR-to-YouTube",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX-packed exes trip antivirus heuristics
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,           # windowed app, no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="TwitchDVR-to-YouTube.app",
        icon=None,
        bundle_identifier="com.froncu.twitchdvr2yt",
        version=__version__,
        info_plist={
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "CFBundleDisplayName": "TwitchDVR to YouTube",
        },
    )
