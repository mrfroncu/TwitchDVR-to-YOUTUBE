"""Tkinter GUI: Videos / Queue & Progress / Settings tabs."""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import auth, config, scanner
from .scanner import Vod
from .uploader import QueueItem, UploadWorker, verify_video
from .version import __version__

try:
    from send2trash import send2trash
except ImportError:      # running from source without the dependency
    send2trash = None

try:
    import sv_ttk
except ImportError:      # theme is optional; the app still works unthemed
    sv_ttk = None

CHECKED, UNCHECKED = "☑", "☐"

THEME_COLORS = {
    "dark": {"ok": "#5ecb63", "err": "#ff7069", "info": "#67b7ff",
             "muted": "#9a9a9a", "field_bg": "#2a2a2a", "fg": "#fafafa"},
    "light": {"ok": "#2e7d32", "err": "#b71c1c", "info": "#1565c0",
              "muted": "#666666", "field_bg": "#ffffff", "fg": "#1c1c1c"},
}

POLL_MS = 100


def open_in_file_manager(path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n} B"


def fmt_speed(bps: float) -> str:
    return fmt_size(int(bps)) + "/s"


def fmt_eta(seconds: float) -> str:
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    if s >= 60:
        return f"{s // 60}m {s % 60}s"
    return f"{s}s"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = config.load_config()
        self.registry = config.load_registry()

        self.vods: dict[str, Vod] = {}
        self.metas: dict[str, dict] = {}     # per-VOD editable metadata
        self.queue_items: list[QueueItem] = []
        self.worker: UploadWorker | None = None
        self.events: queue.Queue = queue.Queue()
        self.credentials = None
        self.channel: dict | None = None
        self._editing_key: str | None = None

        root.title(f"TwitchDVR to YouTube Uploader  v{__version__}")
        root.geometry("1240x820")
        root.minsize(1000, 640)

        self.colors = THEME_COLORS.get(self.cfg.get("theme", "dark"),
                                       THEME_COLORS["dark"])
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=6, pady=(6, 0))
        self._build_videos_tab()
        self._build_queue_tab()
        self._build_settings_tab()
        self._build_status_bar()
        self._apply_theme(self.cfg.get("theme", "dark"))

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(POLL_MS, self._poll_events)

        # Try silent sign-in with the saved token
        if config.TOKEN_PATH.exists():
            self._log("Restoring saved YouTube session…")
            threading.Thread(target=self._restore_session, daemon=True).start()
        if self.cfg.get("vod_folder") and Path(self.cfg["vod_folder"]).is_dir():
            self.folder_var.set(self.cfg["vod_folder"])
            self.root.after(200, self.scan_folder)

    # ----------------------------------------------------------------- theme --
    def _apply_theme(self, theme: str) -> None:
        theme = theme if theme in THEME_COLORS else "dark"
        self.colors = THEME_COLORS[theme]
        if sv_ttk is not None:
            sv_ttk.set_theme(theme)
        style = ttk.Style()
        style.configure("Muted.TLabel", foreground=self.colors["muted"])
        for tree in (self.video_tree, self.queue_tree):
            tree.tag_configure("uploaded", foreground=self.colors["ok"])
            tree.tag_configure("done", foreground=self.colors["ok"])
            tree.tag_configure("problem", foreground=self.colors["err"])
            tree.tag_configure("error", foreground=self.colors["err"])
            tree.tag_configure("uploading", foreground=self.colors["info"])
        for txt in (self.desc_text, self.log_text):
            txt.configure(bg=self.colors["field_bg"], fg=self.colors["fg"],
                          insertbackground=self.colors["fg"],
                          relief="flat", highlightthickness=0)
        self._set_titlebar_dark(theme == "dark")
        self._update_title_count()

    def _set_titlebar_dark(self, dark: bool) -> None:
        if sys.platform != "win32":
            return
        try:
            from ctypes import byref, c_int, windll
            self.root.update_idletasks()
            hwnd = windll.user32.GetParent(self.root.winfo_id())
            # DWMWA_USE_IMMERSIVE_DARK_MODE
            windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, byref(c_int(int(dark))), 4)
            # nudge a repaint so the title bar updates immediately
            self.root.attributes("-alpha", 0.99)
            self.root.attributes("-alpha", 1.0)
        except Exception:
            pass

    def _on_theme_change(self, _event=None) -> None:
        theme = self.theme_var.get()
        self.cfg["theme"] = theme
        config.save_config(self.cfg)
        self._apply_theme(theme)

    # ------------------------------------------------------------ videos tab --
    def _build_videos_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Videos  ")

        top = ttk.Frame(tab)
        top.pack(fill="x", padx=8, pady=8)
        ttk.Label(top, text="VOD folder:").pack(side="left")
        self.folder_var = tk.StringVar(value=self.cfg.get("vod_folder", ""))
        ttk.Entry(top, textvariable=self.folder_var).pack(
            side="left", fill="x", expand=True, padx=6)
        ttk.Button(top, text="Browse…", command=self.pick_folder).pack(side="left")
        ttk.Button(top, text="Scan", command=self.scan_folder).pack(side="left", padx=(6, 0))

        cols = ("check", "date", "streamer", "title", "duration", "size",
                "chapters", "status")
        self.video_tree = ttk.Treeview(tab, columns=cols, show="headings",
                                       selectmode="extended", height=10)
        headings = {"check": (UNCHECKED, 36), "date": ("Date", 90),
                    "streamer": ("Streamer", 100),
                    "title": ("Stream title", 300), "duration": ("Length", 70),
                    "size": ("Size", 80), "chapters": ("Chapters", 70),
                    "status": ("Status", 150)}
        for col, (text, width) in headings.items():
            self.video_tree.heading(col, text=text)
            self.video_tree.column(col, width=width,
                                   anchor="center" if col not in ("title",) else "w")
        self.video_tree.heading("check", command=self._toggle_all_videos)
        self.video_checked: set[str] = set()
        vsb = ttk.Scrollbar(tab, orient="vertical", command=self.video_tree.yview)
        self.video_tree.configure(yscrollcommand=vsb.set)
        self.video_tree.pack(side="top", fill="both", expand=False, padx=(8, 0))
        vsb.place(in_=self.video_tree, relx=1.0, rely=0, relheight=1.0, anchor="ne")
        self.video_tree.bind("<Button-1>", self._on_video_tree_click)
        self.video_tree.bind("<<TreeviewSelect>>", self._on_video_select)
        self.video_tree.bind("<Double-1>", self._open_vod_folder)

        # ---- bulk actions on checked rows
        bulk = ttk.LabelFrame(tab, text="Bulk actions (apply to checked rows)")
        bulk.pack(fill="x", padx=8, pady=(6, 0))
        row = ttk.Frame(bulk)
        row.pack(fill="x", padx=6, pady=6)
        ttk.Button(row, text="Check all", width=9,
                   command=lambda: self._set_all_videos_checked(True)).pack(side="left")
        ttk.Button(row, text="None", width=6,
                   command=lambda: self._set_all_videos_checked(False)).pack(
            side="left", padx=(4, 10))
        ttk.Button(row, text="Add to queue ▶", style="Accent.TButton",
                   command=self.bulk_add_checked).pack(side="left")
        ttk.Button(row, text="Reset metadata",
                   command=self.bulk_reset_meta).pack(side="left", padx=4)
        ttk.Label(row, text="Privacy:").pack(side="left", padx=(10, 2))
        self.bulk_privacy_var = tk.StringVar(value=self.cfg["privacy"])
        ttk.Combobox(row, textvariable=self.bulk_privacy_var, width=9, state="readonly",
                     values=("private", "unlisted", "public")).pack(side="left")
        ttk.Button(row, text="Apply",
                   command=self.bulk_apply_privacy).pack(side="left", padx=(2, 10))
        row2 = ttk.Frame(bulk)
        row2.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(row2, text="Verify on YouTube",
                   command=self.bulk_verify).pack(side="left")
        ttk.Button(row2, text="🗑 Recycle local files",
                   command=self.bulk_recycle).pack(side="left", padx=4)
        ttk.Button(row2, text="Reset upload state",
                   command=self.bulk_reset_state).pack(side="left")

        # ---- metadata editor
        editor = ttk.LabelFrame(tab, text="Video metadata (edit before queueing)")
        editor.pack(fill="both", expand=True, padx=8, pady=8)

        row1 = ttk.Frame(editor)
        row1.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(row1, text="Title:").pack(side="left")
        self.title_var = tk.StringVar()
        self.title_var.trace_add("write", lambda *_: self._update_title_count())
        ttk.Entry(row1, textvariable=self.title_var).pack(
            side="left", fill="x", expand=True, padx=6)
        self.title_count = ttk.Label(row1, text="0/100", width=8)
        self.title_count.pack(side="left")

        row2 = ttk.Frame(editor)
        row2.pack(fill="x", padx=6, pady=2)
        ttk.Label(row2, text="Tags (comma separated):").pack(side="left")
        self.tags_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.tags_var).pack(
            side="left", fill="x", expand=True, padx=6)
        ttk.Label(row2, text="Privacy:").pack(side="left")
        self.privacy_var = tk.StringVar(value=self.cfg["privacy"])
        ttk.Combobox(row2, textvariable=self.privacy_var, width=9, state="readonly",
                     values=("private", "unlisted", "public")).pack(side="left", padx=(4, 0))

        ttk.Label(editor, text="Description (chapter timestamps become YouTube chapters):"
                  ).pack(anchor="w", padx=6, pady=(4, 0))
        desc_frame = ttk.Frame(editor)
        desc_frame.pack(fill="both", expand=True, padx=6, pady=(2, 4))
        self.desc_text = tk.Text(desc_frame, height=8, wrap="word", undo=True)
        dsb = ttk.Scrollbar(desc_frame, orient="vertical", command=self.desc_text.yview)
        self.desc_text.configure(yscrollcommand=dsb.set)
        self.desc_text.pack(side="left", fill="both", expand=True)
        dsb.pack(side="left", fill="y")

        btns = ttk.Frame(editor)
        btns.pack(fill="x", padx=6, pady=(0, 8))
        ttk.Button(btns, text="Reset to generated metadata",
                   command=self._regenerate_selected).pack(side="left")
        ttk.Button(btns, text="Add selected to queue ▶", style="Accent.TButton",
                   command=self.add_selected_to_queue).pack(side="right")

    # ------------------------------------------------------------- queue tab --
    def _build_queue_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Queue & Progress  ")

        cols = ("check", "pos", "title", "size", "privacy", "status", "detail")
        self.queue_tree = ttk.Treeview(tab, columns=cols, show="headings",
                                       selectmode="browse", height=12)
        headings = {"check": (UNCHECKED, 36), "pos": ("#", 40),
                    "title": ("Title", 330), "size": ("Size", 85),
                    "privacy": ("Privacy", 70), "status": ("Status", 90),
                    "detail": ("Progress / result", 280)}
        for col, (text, width) in headings.items():
            self.queue_tree.heading(col, text=text)
            self.queue_tree.column(col, width=width,
                                   anchor="center" if col in ("check", "pos", "size", "privacy", "status") else "w")
        self.queue_tree.heading("check", command=self._toggle_all_queue)
        self.queue_checked: set[str] = set()
        self.queue_tree.bind("<Button-1>", self._on_queue_tree_click)
        self.queue_tree.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        ctl = ttk.Frame(tab)
        ctl.pack(fill="x", padx=8, pady=4)
        self.start_btn = ttk.Button(ctl, text="▶ Start uploads", style="Accent.TButton",
                                    command=self.start_uploads)
        self.start_btn.pack(side="left")
        self.pause_btn = ttk.Button(ctl, text="⏸ Pause after current",
                                    command=self.pause_uploads, state="disabled")
        self.pause_btn.pack(side="left", padx=6)
        self.cancel_btn = ttk.Button(ctl, text="✖ Cancel current upload",
                                     command=self.cancel_current, state="disabled")
        self.cancel_btn.pack(side="left")
        ttk.Button(ctl, text="Remove checked/selected", command=self.remove_queue_item
                   ).pack(side="right")
        ttk.Button(ctl, text="▼", width=3, command=lambda: self.move_queue_item(1)
                   ).pack(side="right", padx=(0, 6))
        ttk.Button(ctl, text="▲", width=3, command=lambda: self.move_queue_item(-1)
                   ).pack(side="right")
        ttk.Button(ctl, text="Clear finished", command=self.clear_finished
                   ).pack(side="right", padx=6)

        prog = ttk.Frame(tab)
        prog.pack(fill="x", padx=8, pady=4)
        self.current_label = ttk.Label(prog, text="Idle.")
        self.current_label.pack(anchor="w")
        self.progressbar = ttk.Progressbar(prog, maximum=100.0)
        self.progressbar.pack(fill="x", pady=(2, 0))

        ttk.Label(tab, text="Log:").pack(anchor="w", padx=8)
        log_frame = ttk.Frame(tab)
        log_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log_text = tk.Text(log_frame, height=7, wrap="word", state="disabled")
        lsb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lsb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        lsb.pack(side="left", fill="y")

    # ---------------------------------------------------------- settings tab --
    def _build_settings_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Settings  ")

        looks = ttk.LabelFrame(tab, text="Appearance")
        looks.pack(fill="x", padx=10, pady=(10, 0))
        row = ttk.Frame(looks)
        row.pack(fill="x", padx=8, pady=8)
        ttk.Label(row, text="Theme:").pack(side="left")
        self.theme_var = tk.StringVar(value=self.cfg.get("theme", "dark"))
        theme_box = ttk.Combobox(row, textvariable=self.theme_var, width=8,
                                 state="readonly", values=("dark", "light"))
        theme_box.pack(side="left", padx=6)
        theme_box.bind("<<ComboboxSelected>>", self._on_theme_change)

        acct = ttk.LabelFrame(tab, text="YouTube account")
        acct.pack(fill="x", padx=10, pady=10)

        row = ttk.Frame(acct)
        row.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(row, text="OAuth client secret file:").pack(side="left")
        self.secret_var = tk.StringVar(value=self.cfg.get("client_secret_path", ""))
        ttk.Entry(row, textvariable=self.secret_var).pack(
            side="left", fill="x", expand=True, padx=6)
        ttk.Button(row, text="Browse…", command=self._pick_secret).pack(side="left")

        row = ttk.Frame(acct)
        row.pack(fill="x", padx=8, pady=2)
        ttk.Label(row, text="Local redirect port (127.0.0.1):").pack(side="left")
        self.port_var = tk.StringVar(value=str(self.cfg.get("oauth_port", 8710)))
        ttk.Entry(row, textvariable=self.port_var, width=8).pack(side="left", padx=6)
        ttk.Label(row, text="(OAuth client type must be “Desktop app” — any port works)"
                  ).pack(side="left")

        row = ttk.Frame(acct)
        row.pack(fill="x", padx=8, pady=(4, 8))
        self.signin_btn = ttk.Button(row, text="Sign in with Google…",
                                     style="Accent.TButton", command=self.sign_in)
        self.signin_btn.pack(side="left")
        ttk.Button(row, text="Sign out", command=self.sign_out).pack(side="left", padx=6)
        self.account_label = ttk.Label(row, text="Not signed in.")
        self.account_label.pack(side="left", padx=10)

        hint = ("The browser sign-in page is where you pick the Google account AND the "
                "YouTube channel (brand accounts are listed there). To connect a different "
                "channel: Sign out, then Sign in again.")
        ttk.Label(acct, text=hint, wraplength=900, style="Muted.TLabel"
                  ).pack(anchor="w", padx=8, pady=(0, 8))

        up = ttk.LabelFrame(tab, text="Upload defaults")
        up.pack(fill="x", padx=10, pady=(0, 10))

        row = ttk.Frame(up)
        row.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(row, text="Default privacy:").pack(side="left")
        self.def_privacy_var = tk.StringVar(value=self.cfg["privacy"])
        ttk.Combobox(row, textvariable=self.def_privacy_var, width=10, state="readonly",
                     values=("private", "unlisted", "public")).pack(side="left", padx=6)
        ttk.Label(row, text="Category:").pack(side="left", padx=(14, 0))
        inv = {v: k for k, v in config.CATEGORIES.items()}
        self.category_var = tk.StringVar(
            value=inv.get(str(self.cfg.get("category_id", "20")), "Gaming (20)"))
        ttk.Combobox(row, textvariable=self.category_var, width=24, state="readonly",
                     values=list(config.CATEGORIES)).pack(side="left", padx=6)

        row = ttk.Frame(up)
        row.pack(fill="x", padx=8, pady=2)
        ttk.Label(row, text="Title template:").pack(side="left")
        self.template_var = tk.StringVar(value=self.cfg["title_template"])
        ttk.Entry(row, textvariable=self.template_var).pack(
            side="left", fill="x", expand=True, padx=6)
        ttk.Label(up, text="Placeholders: {title} {streamer} {login} {date} {game} {games}",
                  style="Muted.TLabel").pack(anchor="w", padx=8)

        row = ttk.Frame(up)
        row.pack(fill="x", padx=8, pady=2)
        ttk.Label(row, text="After verified upload:").pack(side="left")
        inv_after = {v: k for k, v in config.AFTER_UPLOAD_CHOICES.items()}
        self.after_upload_var = tk.StringVar(
            value=inv_after.get(self.cfg.get("after_upload", "keep"),
                                "Keep local files"))
        ttk.Combobox(row, textvariable=self.after_upload_var, width=34,
                     state="readonly",
                     values=list(config.AFTER_UPLOAD_CHOICES)).pack(side="left", padx=6)
        ttk.Label(row, text="(only after YouTube confirms the video exists; "
                            "files go to the Recycle Bin)",
                  style="Muted.TLabel").pack(side="left")

        row = ttk.Frame(up)
        row.pack(fill="x", padx=8, pady=(6, 8))
        self.notify_var = tk.BooleanVar(value=bool(self.cfg.get("notify_subscribers", False)))
        ttk.Checkbutton(row, text="Notify subscribers on upload",
                        variable=self.notify_var).pack(side="left")
        self.kids_var = tk.BooleanVar(value=bool(self.cfg.get("made_for_kids", False)))
        ttk.Checkbutton(row, text="Mark as “made for kids”",
                        variable=self.kids_var).pack(side="left", padx=14)
        ttk.Label(row, text="Upload chunk size (MB):").pack(side="left", padx=(14, 0))
        self.chunk_var = tk.StringVar(value=str(self.cfg.get("chunk_mb", 64)))
        ttk.Entry(row, textvariable=self.chunk_var, width=5).pack(side="left", padx=4)
        ttk.Label(row, text="(64–256 recommended on fast connections; each chunk "
                            "is one request, so bigger = faster)",
                  style="Muted.TLabel").pack(side="left")

        ttk.Button(tab, text="Save settings", command=self.save_settings
                   ).pack(anchor="e", padx=10)

        notes = (
            "Quota note: every upload costs 1600 API units and Google's default daily quota "
            "is 10,000 units, i.e. about 6 uploads per day. The queue pauses automatically "
            "when quota runs out (it resets at midnight Pacific time).\n"
            "Important: while your Google Cloud OAuth app is unverified / in testing mode, "
            "videos uploaded through the API are locked to PRIVATE by YouTube. Complete the "
            "API audit/verification to allow public uploads.")
        ttk.Label(tab, text=notes, wraplength=940, style="Muted.TLabel", justify="left"
                  ).pack(anchor="w", padx=10, pady=10)

    def _build_status_bar(self) -> None:
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", side="bottom")
        ttk.Separator(bar, orient="horizontal").pack(fill="x")
        inner = ttk.Frame(bar)
        inner.pack(fill="x", padx=8, pady=3)
        self.status_channel = ttk.Label(inner, text="Not signed in")
        self.status_channel.pack(side="left")
        self.status_queue = ttk.Label(inner, text="")
        self.status_queue.pack(side="right")

    # -------------------------------------------------------------- scanning --
    def pick_folder(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.folder_var.get() or None,
                                         title="Pick the folder that contains the VOD subfolders")
        if folder:
            self.folder_var.set(folder)
            self.scan_folder()

    def scan_folder(self) -> None:
        folder = self.folder_var.get().strip()
        if not folder:
            return
        root = Path(folder)
        if not root.is_dir():
            messagebox.showerror("Scan", f"Folder does not exist:\n{folder}")
            return
        self.cfg["vod_folder"] = folder
        config.save_config(self.cfg)

        self._save_editor()
        self._editing_key = None
        vods = scanner.scan_folder(root)
        self.vods = {v.key: v for v in vods}
        self.video_checked &= set(self.vods)
        for key in list(self.metas):
            if key not in self.vods:
                del self.metas[key]
        for vod in vods:
            if vod.key not in self.metas:
                self.metas[vod.key] = self._generate_meta(vod)
        self._refresh_video_tree()
        self._log(f"Scanned {folder}: found {len(vods)} VOD folder(s).")

    def _generate_meta(self, vod: Vod) -> dict:
        return {
            "title": scanner.build_title(vod, self.template_var.get()
                                         if hasattr(self, "template_var")
                                         else self.cfg["title_template"]),
            "description": scanner.build_description(vod),
            "tags": ", ".join(scanner.build_tags(vod)),
            "privacy": self.cfg["privacy"],
        }

    def _video_status(self, vod: Vod) -> tuple[str, str]:
        """(status text, tree tag)"""
        entry = self.registry.get(vod.key)
        if entry:
            if entry.get("failed"):
                return "failed on YouTube — requeue or reset", "problem"
            if entry.get("local_deleted"):
                return "uploaded ✓ · recycled", "uploaded"
            if entry.get("verified"):
                return "uploaded ✓ verified", "uploaded"
            return "uploaded (unverified)", "uploaded"
        for item in self.queue_items:
            if item.key == vod.key and item.status in ("queued", "uploading", "verifying"):
                return item.status, ""
        if vod.problems:
            return ", ".join(vod.problems), "problem"
        return "ready", ""

    def _refresh_video_tree(self) -> None:
        selected = set(self.video_tree.selection())
        self.video_tree.delete(*self.video_tree.get_children())
        for vod in self.vods.values():
            status, tag = self._video_status(vod)
            dur = ""
            if vod.duration:
                h, rem = divmod(int(vod.duration), 3600)
                dur = f"{h}:{rem // 60:02d}:{rem % 60:02d}"
            self.video_tree.insert(
                "", "end", iid=vod.key, tags=(tag,) if tag else (),
                values=(CHECKED if vod.key in self.video_checked else UNCHECKED,
                        vod.date_str, vod.streamer_name or vod.streamer_login,
                        vod.stream_title, dur,
                        fmt_size(vod.size_bytes) if vod.size_bytes else "—",
                        len(vod.chapters), status))
        for iid in selected:
            if self.video_tree.exists(iid):
                self.video_tree.selection_add(iid)

    # ------------------------------------------------------------ checkboxes --
    def _on_video_tree_click(self, event):
        if self.video_tree.identify("region", event.x, event.y) == "cell" and \
                self.video_tree.identify_column(event.x) == "#1":
            key = self.video_tree.identify_row(event.y)
            if key:
                self.video_checked.symmetric_difference_update({key})
                self.video_tree.set(
                    key, "check",
                    CHECKED if key in self.video_checked else UNCHECKED)
            return "break"
        return None

    def _set_all_videos_checked(self, checked: bool) -> None:
        self.video_checked = set(self.vods) if checked else set()
        for key in self.video_tree.get_children():
            self.video_tree.set(key, "check", CHECKED if checked else UNCHECKED)

    def _toggle_all_videos(self) -> None:
        self._set_all_videos_checked(len(self.video_checked) < len(self.vods))

    def _checked_video_keys(self) -> list[str]:
        return [k for k in self.video_tree.get_children() if k in self.video_checked]

    # -------------------------------------------------------------- bulk ops --
    def bulk_add_checked(self) -> None:
        self._save_editor()
        keys = self._checked_video_keys()
        if not keys:
            messagebox.showinfo("Bulk", "No rows checked — click the ☐ column first.")
            return
        self._enqueue(keys)

    def bulk_reset_meta(self) -> None:
        keys = self._checked_video_keys()
        for key in keys:
            self.metas[key] = self._generate_meta(self.vods[key])
        if self._editing_key in keys:
            self._editing_key = None
            self._on_video_select()
        self._log(f"Reset metadata for {len(keys)} video(s).")

    def bulk_apply_privacy(self) -> None:
        privacy = self.bulk_privacy_var.get()
        keys = self._checked_video_keys()
        for key in keys:
            self.metas[key]["privacy"] = privacy
        if self._editing_key in keys:
            self.privacy_var.set(privacy)
        self._log(f"Set privacy '{privacy}' on {len(keys)} video(s).")

    def bulk_verify(self) -> None:
        keys = [k for k in self._checked_video_keys()
                if self.registry.get(k, {}).get("video_id")]
        if not keys:
            messagebox.showinfo(
                "Verify", "None of the checked rows have been uploaded yet — "
                "there is nothing to verify.")
            return
        if self.credentials is None:
            self.credentials = auth.load_credentials()
        if self.credentials is None:
            messagebox.showwarning("Verify", "Not signed in to YouTube.")
            return
        creds = self.credentials
        self._log(f"Verifying {len(keys)} upload(s) on YouTube…")

        def worker():
            try:
                service = auth.build_service(creds)
            except Exception as exc:
                self.events.put({"type": "log", "text": f"Verify failed: {exc}"})
                return
            for key in keys:
                video_id = self.registry[key]["video_id"]
                try:
                    ok, detail = verify_video(service, video_id)
                except Exception as exc:
                    ok, detail = None, str(exc)[:200]
                self.events.put({"type": "verify_result", "key": key,
                                 "ok": ok, "detail": detail,
                                 "video_id": video_id})

        threading.Thread(target=worker, daemon=True).start()

    def bulk_recycle(self) -> None:
        mode = self.cfg.get("after_upload", "keep")
        if mode == "keep":
            mode = "trash_video"   # manual action defaults to video file only
        candidates = []
        for key in self._checked_video_keys():
            entry = self.registry.get(key)
            if entry and entry.get("verified") and not entry.get("local_deleted"):
                candidates.append(key)
        if not candidates:
            messagebox.showinfo(
                "Recycle", "Nothing to recycle. Only checked videos that were "
                "uploaded AND verified on YouTube can be recycled.\n"
                "Use “Verify on YouTube” first if needed.")
            return
        what = "whole VOD folders" if mode == "trash_folder" else "video files"
        if not messagebox.askyesno(
                "Recycle", f"Move the {what} of {len(candidates)} verified "
                "upload(s) to the Recycle Bin?"):
            return
        done = 0
        for key in candidates:
            if self._recycle_vod(key, mode):
                done += 1
        self._log(f"Recycled local files of {done} upload(s).")
        self.scan_folder()

    def bulk_reset_state(self) -> None:
        """Forget the upload record of checked rows so they can be re-uploaded."""
        keys = [k for k in self._checked_video_keys() if k in self.registry]
        if not keys:
            messagebox.showinfo(
                "Reset", "None of the checked rows have an upload record to reset.")
            return
        if not messagebox.askyesno(
                "Reset upload state",
                f"Forget the upload record of {len(keys)} video(s)?\n\n"
                "They will show as 'ready' again and can be re-uploaded. "
                "Videos already on YouTube are NOT touched — this only clears "
                "the app's own bookkeeping, so re-uploading may create duplicates."):
            return
        for key in keys:
            del self.registry[key]
        config.save_registry(self.registry)
        self._refresh_video_tree()
        self._log(f"Reset upload state of {len(keys)} video(s) — they can be queued again.")

    def _recycle_vod(self, key: str, mode: str) -> bool:
        if send2trash is None:
            self._log("Recycle unavailable: the 'Send2Trash' package is not "
                      "installed (pip install Send2Trash).")
            return False
        vod = self.vods.get(key)
        item = self._item_by_key(key)
        if vod is None and item is not None:
            vod = item.vod
        if vod is None:
            return False
        target = vod.folder if mode == "trash_folder" else vod.video_path
        if target is None or not target.exists():
            return False
        try:
            send2trash(str(target))
        except Exception as exc:
            self._log(f"Could not recycle {target}: {exc}")
            return False
        entry = self.registry.setdefault(key, {})
        entry["local_deleted"] = True
        config.save_registry(self.registry)
        self._log(f"Moved to Recycle Bin: {target}")
        return True

    def _open_vod_folder(self, _event) -> None:
        sel = self.video_tree.selection()
        if sel and sel[0] in self.vods:
            open_in_file_manager(self.vods[sel[0]].folder)

    # ---------------------------------------------------------------- editor --
    def _on_video_select(self, _event=None) -> None:
        self._save_editor()
        sel = self.video_tree.selection()
        if len(sel) != 1 or sel[0] not in self.vods:
            self._editing_key = None
            return
        key = sel[0]
        meta = self.metas[key]
        self._editing_key = None  # suppress saves while loading fields
        self.title_var.set(meta["title"])
        self.tags_var.set(meta["tags"])
        self.privacy_var.set(meta["privacy"])
        self.desc_text.delete("1.0", "end")
        self.desc_text.insert("1.0", meta["description"])
        self._editing_key = key

    def _save_editor(self) -> None:
        if not self._editing_key or self._editing_key not in self.metas:
            return
        meta = self.metas[self._editing_key]
        meta["title"] = scanner.sanitize_title(self.title_var.get())
        meta["tags"] = self.tags_var.get().strip()
        meta["privacy"] = self.privacy_var.get()
        meta["description"] = self.desc_text.get("1.0", "end-1c")

    def _update_title_count(self) -> None:
        n = len(self.title_var.get())
        self.title_count.configure(
            text=f"{n}/100", foreground=self.colors["err"] if n > 100 else "")

    def _regenerate_selected(self) -> None:
        sel = self.video_tree.selection()
        for key in sel:
            if key in self.vods:
                self.metas[key] = self._generate_meta(self.vods[key])
        if len(sel) == 1:
            self._editing_key = None
            self._on_video_select()

    # ----------------------------------------------------------------- queue --
    def add_selected_to_queue(self) -> None:
        self._save_editor()
        keys = [k for k in self.video_tree.selection() if k in self.vods]
        self._enqueue(keys)

    def _enqueue(self, keys: list[str]) -> None:
        added = skipped = 0
        queued_keys = {i.key for i in self.queue_items
                       if i.status in ("queued", "uploading", "verifying", "done")}
        for key in keys:
            vod = self.vods[key]
            entry = self.registry.get(key)
            # A registry entry blocks re-upload unless that upload is known
            # to have failed on YouTube's side.
            if (entry and not entry.get("failed")) or key in queued_keys:
                skipped += 1
                continue
            if vod.video_path is None:
                skipped += 1
                self._log(f"Skipped {key}: no video file found.")
                continue
            meta = self.metas[key]
            tags = [t.strip() for t in meta["tags"].split(",") if t.strip()]
            self.queue_items.append(QueueItem(
                vod=vod,
                title=scanner.sanitize_title(meta["title"]),
                description=meta["description"],
                tags=tags,
                privacy=meta["privacy"],
                category_id=str(self.cfg.get("category_id", "20")),
                recording_date=scanner.recording_date(vod),
                notify_subscribers=bool(self.cfg.get("notify_subscribers", False)),
                made_for_kids=bool(self.cfg.get("made_for_kids", False)),
            ))
            added += 1
        self._refresh_queue_tree()
        self._refresh_video_tree()
        if added:
            self.notebook.select(1)
        self._log(f"Queued {added} video(s)" +
                  (f", skipped {skipped} (already uploaded/queued or unusable)." if skipped else "."))

    def _refresh_queue_tree(self) -> None:
        self.queue_checked &= {i.key for i in self.queue_items}
        self.queue_tree.delete(*self.queue_tree.get_children())
        for idx, item in enumerate(self.queue_items, start=1):
            detail = item.detail
            if item.status == "uploading" and item.progress:
                detail = f"{item.progress:.1f}%  {item.detail}"
            tag = item.status if item.status in ("done", "error", "uploading") else ""
            self.queue_tree.insert(
                "", "end", iid=item.key, tags=(tag,) if tag else (),
                values=(CHECKED if item.key in self.queue_checked else UNCHECKED,
                        idx, item.title, fmt_size(item.vod.size_bytes),
                        item.privacy, item.status, detail))
        pending = sum(1 for i in self.queue_items if i.status == "queued")
        done = sum(1 for i in self.queue_items if i.status == "done")
        self.status_queue.configure(
            text=f"Queue: {pending} pending, {done} done, {len(self.queue_items)} total")

    def _on_queue_tree_click(self, event):
        if self.queue_tree.identify("region", event.x, event.y) == "cell" and \
                self.queue_tree.identify_column(event.x) == "#1":
            key = self.queue_tree.identify_row(event.y)
            if key:
                self.queue_checked.symmetric_difference_update({key})
                self.queue_tree.set(
                    key, "check",
                    CHECKED if key in self.queue_checked else UNCHECKED)
            return "break"
        return None

    def _toggle_all_queue(self) -> None:
        all_keys = {i.key for i in self.queue_items}
        self.queue_checked = set() if self.queue_checked >= all_keys else all_keys
        for key in self.queue_tree.get_children():
            self.queue_tree.set(
                key, "check", CHECKED if key in self.queue_checked else UNCHECKED)

    def _selected_queue_index(self) -> int | None:
        sel = self.queue_tree.selection()
        if not sel:
            return None
        for i, item in enumerate(self.queue_items):
            if item.key == sel[0]:
                return i
        return None

    def remove_queue_item(self) -> None:
        """Remove the checked items, or the selected one if none are checked."""
        keys = set(self.queue_checked)
        if not keys:
            sel = self.queue_tree.selection()
            if not sel:
                return
            keys = {sel[0]}
        busy = [i for i in self.queue_items
                if i.key in keys and i.status in ("uploading", "verifying")]
        if busy:
            messagebox.showinfo("Queue", "A checked video is currently uploading — "
                                "use “Cancel current upload” first.")
            keys -= {i.key for i in busy}
        self.queue_items = [i for i in self.queue_items if i.key not in keys]
        self._refresh_queue_tree()
        self._refresh_video_tree()

    def move_queue_item(self, delta: int) -> None:
        idx = self._selected_queue_index()
        if idx is None:
            return
        new = idx + delta
        if not (0 <= new < len(self.queue_items)):
            return
        items = self.queue_items
        items[idx], items[new] = items[new], items[idx]
        self._refresh_queue_tree()
        self.queue_tree.selection_set(items[new].key)

    def clear_finished(self) -> None:
        self.queue_items = [i for i in self.queue_items
                            if i.status not in ("done", "cancelled")]
        self._refresh_queue_tree()

    # --------------------------------------------------------------- uploads --
    def start_uploads(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not any(i.status == "queued" for i in self.queue_items):
            messagebox.showinfo("Upload", "Queue is empty — add videos on the Videos tab.")
            return
        if self.credentials is None:
            self.credentials = auth.load_credentials()
        if self.credentials is None:
            messagebox.showwarning(
                "Upload", "Not signed in to YouTube.\nGo to Settings → Sign in with Google.")
            self.notebook.select(2)
            return
        # reset stuck error items? leave them; only 'queued' get uploaded
        try:
            chunk_mb = max(1, min(1024, int(self.chunk_var.get())))
        except ValueError:
            chunk_mb = 64
        self.worker = UploadWorker(self.credentials, self.queue_items,
                                   self.events, chunk_mb=chunk_mb)
        self.worker.start()
        self.start_btn.configure(state="disabled")
        self.pause_btn.configure(state="normal")
        self.cancel_btn.configure(state="normal")
        self._log("Upload queue started.")

    def pause_uploads(self) -> None:
        if self.worker:
            self.worker.pause_requested.set()
            self.pause_btn.configure(state="disabled")
            self._log("Will pause after the current upload finishes.")

    def cancel_current(self) -> None:
        if self.worker:
            self.worker.cancel_current.set()
            self._log("Cancelling current upload…")

    # ------------------------------------------------------------------ auth --
    def sign_in(self) -> None:
        secret = self.secret_var.get().strip()
        if not secret or not Path(secret).exists():
            messagebox.showwarning(
                "Sign in",
                "Pick your OAuth client secret JSON first.\n\n"
                "Google Cloud Console → APIs & Services → Credentials → "
                "Create credentials → OAuth client ID → Desktop app → download JSON.")
            return
        try:
            port = int(self.port_var.get())
        except ValueError:
            port = 8710
        self.save_settings(silent=True)
        self.signin_btn.configure(state="disabled")
        self.account_label.configure(text="Waiting for browser sign-in…")
        self._log(f"Opening browser for Google sign-in (redirect on 127.0.0.1:{port})…")

        def worker():
            try:
                creds = auth.sign_in(secret, port)
            except Exception as exc:
                self.events.put({"type": "auth_err",
                                 "error": auth.describe_api_error(exc)})
                return
            self.events.put(self._channel_lookup_event(creds))

        threading.Thread(target=worker, daemon=True).start()

    def _channel_lookup_event(self, creds) -> dict:
        """Credentials are valid; the channel lookup may still fail (e.g. the
        YouTube Data API not being enabled) — report that separately."""
        channel = None
        channel_error = None
        try:
            channel = auth.fetch_channel(auth.build_service(creds))
        except Exception as exc:
            channel_error = auth.describe_api_error(exc)
        return {"type": "auth_ok", "creds": creds, "channel": channel,
                "channel_error": channel_error}

    def _restore_session(self) -> None:
        creds = auth.load_credentials()
        if creds is None:
            self.events.put({"type": "auth_err",
                             "error": "saved session expired — sign in again"})
            return
        self.events.put(self._channel_lookup_event(creds))

    def sign_out(self) -> None:
        auth.sign_out()
        self.credentials = None
        self.channel = None
        self.account_label.configure(text="Not signed in.")
        self.status_channel.configure(text="Not signed in")
        self._log("Signed out.")

    # ---------------------------------------------------------------- events --
    def _poll_events(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                self._handle_event(ev)
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._poll_events)

    def _handle_event(self, ev: dict) -> None:
        etype = ev.get("type")
        if etype == "log":
            self._log(ev["text"])
        elif etype == "item_status":
            item = self._item_by_key(ev["key"])
            if item and ev["status"] == "done":
                verified = bool(ev.get("verified"))
                self.registry[item.key] = {
                    "video_id": item.video_id,
                    "title": item.title,
                    "uploaded_at": datetime.now(timezone.utc).isoformat(),
                    "verified": verified,
                }
                config.save_registry(self.registry)
                self.progressbar.configure(value=0)
                self.current_label.configure(text="Idle.")
                mode = self.cfg.get("after_upload", "keep")
                if verified and mode != "keep":
                    if self._recycle_vod(item.key, mode):
                        self._refresh_video_tree()
            if item and ev["status"] == "uploading":
                self.current_label.configure(text=f"Uploading: {item.title}")
                self.progressbar.configure(value=0)
            if item and ev["status"] == "verifying":
                self.current_label.configure(text=f"Verifying on YouTube: {item.title}")
            self._refresh_queue_tree()
            self._refresh_video_tree()
        elif etype == "verify_result":
            entry = self.registry.get(ev["key"])
            if entry is not None:
                if ev["ok"] is not None:
                    entry["verified"] = bool(ev["ok"])
                    entry["failed"] = not ev["ok"]
                entry["verify_detail"] = ev["detail"]
                config.save_registry(self.registry)
            state = {True: "OK", False: "MISSING/FAILED", None: "check unavailable"}[ev["ok"]]
            self._log(f"Verify {ev['key']} (https://youtu.be/{ev['video_id']}): "
                      f"{state} — {ev['detail']}")
            self._refresh_video_tree()
        elif etype == "progress":
            item = self._item_by_key(ev["key"])
            if item:
                item.progress = ev["pct"]
                item.detail = f"{fmt_speed(ev['speed_bps'])}, ETA {fmt_eta(ev['eta_s'])}" \
                    if ev["speed_bps"] else ""
                self.progressbar.configure(value=ev["pct"])
                self.current_label.configure(
                    text=f"Uploading: {item.title} — {ev['pct']:.1f}%"
                         + (f" @ {fmt_speed(ev['speed_bps'])}, ETA {fmt_eta(ev['eta_s'])}"
                            if ev["speed_bps"] else ""))
                self._update_queue_row(item)
        elif etype == "worker_done":
            self.start_btn.configure(state="normal")
            self.pause_btn.configure(state="disabled")
            self.cancel_btn.configure(state="disabled")
            self.progressbar.configure(value=0)
            reason = ev.get("reason")
            self.current_label.configure(
                text={"finished": "Queue finished.", "paused": "Paused.",
                      "quota": "Stopped: daily API quota exhausted."}.get(reason, "Idle."))
            if reason == "quota":
                messagebox.showwarning(
                    "Quota", "YouTube API daily quota is exhausted.\n"
                    "It resets at midnight Pacific time — press Start again after that.")
        elif etype == "auth_ok":
            self.credentials = ev["creds"]
            self.channel = ev.get("channel")
            self.signin_btn.configure(state="normal")
            if ev.get("channel_error"):
                self.account_label.configure(text="Signed in — channel lookup failed")
                self.status_channel.configure(text="Signed in (channel unknown)")
                self._log(f"Signed in, but the channel lookup failed: {ev['channel_error']}")
                messagebox.showwarning(
                    "Signed in, but…",
                    "Google sign-in succeeded, but the YouTube API call failed:\n\n"
                    + ev["channel_error"])
            else:
                name = self.channel["title"] if self.channel else "(no channel on this account)"
                self.account_label.configure(text=f"Signed in — channel: {name}")
                self.status_channel.configure(text=f"YouTube channel: {name}")
                self._log(f"Signed in. Uploading as channel: {name}")
        elif etype == "auth_err":
            self.signin_btn.configure(state="normal")
            self.account_label.configure(text="Not signed in.")
            self._log(f"Sign-in problem: {ev['error']}")
            messagebox.showerror("Sign in failed", ev["error"])

    def _item_by_key(self, key: str):
        for item in self.queue_items:
            if item.key == key:
                return item
        return None

    def _update_queue_row(self, item: QueueItem) -> None:
        if self.queue_tree.exists(item.key):
            detail = f"{item.progress:.1f}%  {item.detail}"
            self.queue_tree.set(item.key, "detail", detail)
            self.queue_tree.set(item.key, "status", item.status)

    # -------------------------------------------------------------- settings --
    def _pick_secret(self) -> None:
        path = filedialog.askopenfilename(
            title="Pick client_secret_*.json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if path:
            self.secret_var.set(path)
            self.save_settings(silent=True)

    def save_settings(self, silent: bool = False) -> None:
        self.cfg["client_secret_path"] = self.secret_var.get().strip()
        try:
            self.cfg["oauth_port"] = int(self.port_var.get())
        except ValueError:
            pass
        self.cfg["privacy"] = self.def_privacy_var.get()
        self.cfg["category_id"] = config.CATEGORIES.get(self.category_var.get(), "20")
        self.cfg["title_template"] = self.template_var.get() or config.DEFAULTS["title_template"]
        self.cfg["notify_subscribers"] = bool(self.notify_var.get())
        self.cfg["made_for_kids"] = bool(self.kids_var.get())
        self.cfg["after_upload"] = config.AFTER_UPLOAD_CHOICES.get(
            self.after_upload_var.get(), "keep")
        try:
            self.cfg["chunk_mb"] = max(1, int(self.chunk_var.get()))
        except ValueError:
            pass
        config.save_config(self.cfg)
        if not silent:
            self._log("Settings saved.")

    # ------------------------------------------------------------------ misc --
    def _log(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {text}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                    "Quit", "An upload is still running — quit anyway?\n"
                    "The partial upload will be discarded."):
                return
            self.worker.cancel_current.set()
            self.worker.pause_requested.set()
        self._save_editor()
        self.save_settings(silent=True)
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)  # crisp text on HiDPI
    except Exception:
        pass
    App(root)
    root.mainloop()
