"""Studio desktop mode: the modern web UI hosted in a native window.

Runs the same FastAPI engine as the Docker version on a random loopback
port and opens it in a pywebview window (Edge WebView2 on Windows, WebKit
on macOS). The classic Tkinter interface stays available via
Settings → ui_mode = classic or the --classic flag.
"""
from __future__ import annotations

import os
import socket
import threading
import time


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def main() -> None:
    os.environ["DVR_DESKTOP"] = "1"
    os.environ.pop("WEB_PASSWORD", None)   # never lock the local window

    # First-run terms dialog (reuses the tkinter one)
    from app import config
    if not (config.APP_DIR / ".accepted").exists():
        import tkinter as tk

        from app.gui import _ensure_agreement, _set_app_icon
        root = tk.Tk()
        _set_app_icon(root)
        root.withdraw()
        accepted = _ensure_agreement(root)
        root.destroy()
        if not accepted:
            return

    import uvicorn
    import webview

    from web.server import create_app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(
        create_app(), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()

    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                break
        except OSError:
            time.sleep(0.1)

    webview.create_window(
        "TwitchDVR to YouTube",
        f"http://127.0.0.1:{port}",
        width=1320, height=880, min_size=(1000, 640),
        background_color="#0d1017")
    webview.start()
    os._exit(0)      # take the uvicorn/worker threads down with the window
