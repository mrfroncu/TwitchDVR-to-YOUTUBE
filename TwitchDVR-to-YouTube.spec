# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: single-file windowed executable.

Build with:  pyinstaller TwitchDVR-to-YouTube.spec --noconfirm
Output:      dist/TwitchDVR-to-YouTube.exe   (Windows)
             dist/TwitchDVR-to-YouTube.app   (macOS)
"""
import os
import sys

from PyInstaller.utils.hooks import (collect_data_files, collect_submodules,
                                     copy_metadata)

spec_dir = os.path.dirname(os.path.abspath(SPEC))
sys.path.insert(0, spec_dir)
from app.version import __version__  # noqa: E402

# googleapiclient needs its bundled API discovery documents and the package
# metadata of the google libs at runtime.
datas = collect_data_files("googleapiclient.discovery_cache")
datas += [(os.path.join(spec_dir, "assets"), "assets")]   # app icon etc.
datas += [(os.path.join(spec_dir, "web", "static"), "web/static")]  # Studio UI
datas += [(os.path.join(spec_dir, "CHANGELOG.md"), ".")]   # About → release notes
for pkg in ("google-api-python-client", "google-auth", "google-auth-oauthlib",
            "google-auth-httplib2"):
    datas += copy_metadata(pkg)

# Studio mode: pywebview picks its platform backend dynamically, and uvicorn
# resolves loop/protocol classes from strings.
hiddenimports = collect_submodules("webview") + collect_submodules("uvicorn")
if sys.platform == "win32":
    hiddenimports += ["pywinstyles"]

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    # onedir .app bundle: no onefile bootloader child process, so macOS shows
    # a single Dock icon (the .icns one) instead of two.
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="TwitchDVR-to-YouTube",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="TwitchDVR-to-YouTube",
    )
    app = BUNDLE(
        coll,
        name="TwitchDVR-to-YouTube.app",
        icon=os.path.join(spec_dir, "assets", "icon.icns"),
        bundle_identifier="com.froncu.twitchdvr2yt",
        version=__version__,
        info_plist={
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "CFBundleDisplayName": "TwitchDVR to YouTube",
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="TwitchDVR-to-YouTube",
        icon=os.path.join(spec_dir, "assets", "icon.ico")
            if sys.platform == "win32" else None,
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
