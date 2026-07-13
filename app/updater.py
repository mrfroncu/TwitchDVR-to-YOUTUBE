"""Self-updater: checks GitHub releases and swaps the portable exe in place.

The swap works without an installer: the new exe is downloaded next to the
running one, then a detached batch script waits for this process to exit,
replaces the file and relaunches it.
"""
from __future__ import annotations

import os
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
    exe_url = None
    for asset in data.get("assets", []):
        if asset.get("name", "").lower().endswith(".exe"):
            exe_url = asset.get("browser_download_url")
    return {"version": tag, "exe_url": exe_url,
            "html_url": data.get("html_url") or RELEASES_PAGE}


def can_self_update() -> bool:
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def apply_update(exe_url: str, progress=None) -> None:
    """Download the new exe and schedule the swap+restart, then the caller
    must exit the process."""
    target = Path(sys.executable)
    new_file = target.with_name(target.stem + ".update.exe")
    with requests.get(exe_url, stream=True, timeout=(30, 300)) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(new_file, "wb") as f:
            for chunk in resp.iter_content(512 * 1024):
                f.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total * 100)

    pid = os.getpid()
    bat = Path(tempfile.gettempdir()) / "twitchdvr2yt_update.bat"
    bat.write_text(
        "@echo off\r\n"
        ":wait\r\n"
        f'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul\r\n'
        "if not errorlevel 1 (\r\n"
        "  timeout /t 1 /nobreak >nul\r\n"
        "  goto wait\r\n"
        ")\r\n"
        f'move /y "{new_file}" "{target}" >nul\r\n'
        f'start "" "{target}"\r\n'
        'del "%~f0"\r\n',
        encoding="mbcs", errors="replace")
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW |
             subprocess.CREATE_NEW_PROCESS_GROUP)
    subprocess.Popen(["cmd", "/c", str(bat)], creationflags=flags,
                     close_fds=True)
