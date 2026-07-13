# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: single-file windowed exe.

Build with:  pyinstaller TwitchDVR-to-YouTube.spec --noconfirm
Output:      dist/TwitchDVR-to-YouTube.exe
"""
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

# googleapiclient needs its bundled API discovery documents and the package
# metadata of the google libs at runtime.
datas = collect_data_files("googleapiclient.discovery_cache")
for pkg in ("google-api-python-client", "google-auth", "google-auth-oauthlib",
            "google-auth-httplib2"):
    datas += copy_metadata(pkg)

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
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
