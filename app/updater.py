"""Self-updater: full update pipeline against GitHub releases.

- check_for_update(): newest release with platform-matched asset + notes
- apply_update(): downloads with progress, then hands over to a detached
  helper script that waits for this process to exit, swaps the app in
  place and relaunches it.

Windows (portable exe): the new exe is downloaded next to the running one
and swapped by a hidden PowerShell script (retries while the file is
briefly locked).

macOS (.app bundle): the release dmg is downloaded, a bash script mounts
it, replaces the whole .app and relaunches it via `open`.
"""
from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

from .version import __version__

API_URL = ("https://api.github.com/repos/mrfroncu/TwitchDVR-to-YOUTUBE/"
           "releases/latest")
RELEASES_PAGE = "https://github.com/mrfroncu/TwitchDVR-to-YOUTUBE/releases"


def _ver_tuple(text: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in nums[:4]) or (0,)


def _pick_asset(assets: list[dict]) -> tuple[str | None, str]:
    """Platform-appropriate download: (url, kind)."""
    if sys.platform == "win32":
        for asset in assets:
            if asset.get("name", "").lower().endswith(".exe"):
                return asset.get("browser_download_url"), "exe"
    elif sys.platform == "darwin" and platform.machine() == "arm64":
        for asset in assets:
            name = asset.get("name", "").lower()
            if name.endswith(".dmg") and "arm64" in name:
                return asset.get("browser_download_url"), "dmg"
    return None, ""


def check_for_update() -> dict | None:
    """Newest release info if it's newer than this build, else None."""
    resp = requests.get(API_URL, timeout=10,
                        headers={"Accept": "application/vnd.github+json"})
    if resp.status_code != 200:
        raise RuntimeError(f"release check failed (HTTP {resp.status_code} — "
                           "is the repository public?)")
    data = resp.json()
    tag = (data.get("tag_name") or "").lstrip("v")
    if not tag:
        return None
    current = __version__.split("-")[0]
    if "dev" in __version__ or _ver_tuple(tag) <= _ver_tuple(current):
        return None
    asset_url, kind = _pick_asset(data.get("assets", []))
    return {
        "version": tag,
        "asset_url": asset_url,
        "kind": kind,
        "notes": (data.get("body") or "").strip()[:2000],
        "html_url": data.get("html_url") or RELEASES_PAGE,
    }


def can_self_update() -> bool:
    if not getattr(sys, "frozen", False):
        return False
    if sys.platform == "win32":
        return True
    return sys.platform == "darwin" and platform.machine() == "arm64"


def _clean_env() -> dict:
    """Environment without PyInstaller bootloader variables.

    A onefile app passes _PYI_*/_MEIPASS2 to child processes; if the update
    helper (and the exe it relaunches) inherits them, the new bootloader
    thinks it's a child and loads python DLLs from the OLD, already-deleted
    _MEI directory -> "Failed to load Python DLL ... _MEIxxxxxx".
    """
    return {key: value for key, value in os.environ.items()
            if not key.startswith("_PYI") and key != "_MEIPASS2"}


def _download(url: str, target: Path, progress=None) -> None:
    with requests.get(url, stream=True, timeout=(30, 300)) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(target, "wb") as f:
            for chunk in resp.iter_content(512 * 1024):
                f.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total * 100)


def apply_update(info: dict, progress=None) -> None:
    """Download + schedule the swap. The caller must exit the process
    right after this returns."""
    if not info.get("asset_url"):
        raise RuntimeError("no downloadable asset for this platform")
    if sys.platform == "win32":
        _apply_windows(info["asset_url"], progress)
    elif sys.platform == "darwin":
        _apply_macos(info["asset_url"], progress)
    else:
        raise RuntimeError("self-update is only available on Windows/macOS")


def _apply_windows(exe_url: str, progress=None) -> None:
    target = Path(sys.executable)
    new_file = target.with_name(target.stem + ".update.exe")
    _download(exe_url, new_file, progress)

    app_name = target.stem
    script = Path(tempfile.gettempdir()) / "twitchdvr2yt_update.ps1"
    script.write_text(f"""
$ErrorActionPreference = 'SilentlyContinue'
$src = '{new_file}'
$dst = '{target}'

# Wait for the Python child AND the onefile bootloader parent to be gone,
# otherwise the exe is still locked and its _MEI temp dir is mid-cleanup.
Wait-Process -Id {os.getpid()} -Timeout 120
for ($i = 0; $i -lt 60; $i++) {{
    if (-not (Get-Process -Name '{app_name}' -ErrorAction SilentlyContinue)) {{ break }}
    Start-Sleep -Milliseconds 500
}}

for ($i = 0; $i -lt 30; $i++) {{
    try {{
        Move-Item -LiteralPath $src -Destination $dst -Force -ErrorAction Stop
        break
    }} catch {{
        Start-Sleep -Milliseconds 500
    }}
}}

# Make sure no PyInstaller bootloader variables leak into the new
# process, or it would try to reuse the old (deleted) _MEI directory.
Get-ChildItem Env: | Where-Object {{ $_.Name -like '_PYI*' -or $_.Name -eq '_MEIPASS2' }} |
    ForEach-Object {{ Remove-Item "Env:$($_.Name)" -ErrorAction SilentlyContinue }}

# Give antivirus real-time scanning a moment before the first launch,
# then retry the launch if the bootloader gets killed mid-extraction.
Start-Sleep -Seconds 2
for ($i = 0; $i -lt 3; $i++) {{
    $p = Start-Process -FilePath $dst -PassThru
    Start-Sleep -Seconds 5
    if ($p -and -not $p.HasExited) {{ break }}
    Start-Sleep -Seconds 3
}}
Remove-Item -LiteralPath $PSCommandPath -Force
""", encoding="utf-8")
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-WindowStyle", "Hidden", "-File", str(script)],
        creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True,
        env=_clean_env())


def _apply_macos(dmg_url: str, progress=None) -> None:
    # .../TwitchDVR-to-YouTube.app/Contents/MacOS/TwitchDVR-to-YouTube
    app_path = Path(sys.executable).resolve().parents[2]
    if app_path.suffix != ".app":
        raise RuntimeError("not running from an .app bundle")
    dmg = Path(tempfile.gettempdir()) / "twitchdvr2yt_update.dmg"
    _download(dmg_url, dmg, progress)

    script = Path(tempfile.gettempdir()) / "twitchdvr2yt_update.sh"
    script.write_text(f"""#!/bin/bash
while kill -0 {os.getpid()} 2>/dev/null; do sleep 1; done
MNT=$(mktemp -d)
hdiutil attach -nobrowse -quiet -mountpoint "$MNT" "{dmg}" || exit 1
NEWAPP=$(ls -d "$MNT"/*.app | head -n 1)
if [ -n "$NEWAPP" ]; then
    rm -rf "{app_path}"
    cp -R "$NEWAPP" "{app_path}"
    xattr -cr "{app_path}" 2>/dev/null
fi
hdiutil detach -quiet "$MNT"
rm -f "{dmg}"
open "{app_path}"
rm -f "$0"
""", encoding="utf-8")
    script.chmod(0o755)
    subprocess.Popen(["/bin/bash", str(script)],
                     start_new_session=True, close_fds=True,
                     env=_clean_env())
