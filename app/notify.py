"""Best-effort OS notifications: macOS via `osascript` (no dependency),
Windows via the lightweight `winotify` package. A notification failure must
never disrupt an upload, so every path here is swallowed silently."""
from __future__ import annotations

import subprocess
import sys


def _osa_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title: str, message: str) -> None:
    try:
        if sys.platform == "darwin":
            script = (f"display notification {_osa_string(message)} "
                      f"with title {_osa_string(title)}")
            subprocess.Popen(["osascript", "-e", script])
        elif sys.platform == "win32":
            from winotify import Notification
            Notification(app_id="TwitchDVR to YouTube", title=title,
                        msg=message).show()
    except Exception:
        pass
