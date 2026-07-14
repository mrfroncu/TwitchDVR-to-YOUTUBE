"""Tkinter GUI: Videos / Queue & Progress / Settings tabs."""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import auth, config, limits, playlists, scanner, updater, ytmanager
from .scanner import Vod
from .uploader import QueueItem, UploadWorker, verify_video
from .version import __version__

try:
    from send2trash import send2trash
except ImportError:      # running from source without the dependency
    send2trash = None

CHECKED, UNCHECKED = "☑", "☐"

# Fluent-inspired palettes applied to the native 'clam' theme. Everything is
# drawn by Tk primitives (no image sprites), so resizing stays fast.
THEME_COLORS = {
    "dark": {
        "bg": "#1f1f1f", "surface": "#2b2b2b", "field_bg": "#2a2a2a",
        "border": "#3d3d3d", "hover": "#383838",
        "fg": "#f0f0f0", "muted": "#9a9a9a",
        "accent": "#0f6fc5", "accent_hover": "#1d80d8", "accent_press": "#0a5ba3",
        "ok": "#5ecb63", "err": "#ff7069", "info": "#67b7ff",
        "odd": "#252525", "titlebar": "#1f1f1f",
    },
    "light": {
        "bg": "#f3f3f3", "surface": "#ffffff", "field_bg": "#ffffff",
        "border": "#d5d5d5", "hover": "#e9e9e9",
        "fg": "#1c1c1c", "muted": "#666666",
        "accent": "#0067c0", "accent_hover": "#1975c5", "accent_press": "#00539b",
        "ok": "#2e7d32", "err": "#b71c1c", "info": "#1565c0",
        "odd": "#fafafa", "titlebar": "#f3f3f3",
    },
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
        self.playlists: list[dict] = []
        self.playlist_ids: dict[str, str] = {}
        self._prog_anim = {"active": False, "pct": 0.0, "pct_per_s": 0.0,
                           "ts": 0.0, "shown": 0.0}
        self._auto_countdown = int(self.cfg.get("auto_scan_interval_min", 10)) * 60

        root.title(f"TwitchDVR to YouTube Uploader  v{__version__}")
        root.geometry("1240x820")
        root.minsize(1000, 640)

        self.colors = THEME_COLORS.get(self.cfg.get("theme", "dark"),
                                       THEME_COLORS["dark"])
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=6, pady=(6, 0))
        self._build_videos_tab()
        self._build_queue_tab()
        self._build_automation_tab()
        self._build_playlists_tab()
        self._build_manager_tab()
        self._build_settings_tab()
        self._build_status_bar()
        self._apply_theme(self.cfg.get("theme", "dark"))

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(POLL_MS, self._poll_events)
        root.after(1000, self._auto_tick)

        # Restore saved account(s) and check for updates in the background
        auth.migrate_legacy_token()
        self.accounts: list[dict] = auth.list_accounts()
        self._populate_account_combos()
        if self.accounts:
            self._log("Restoring saved YouTube session…")
            threading.Thread(target=self._restore_session, daemon=True).start()
        threading.Thread(target=self._update_check_bg, daemon=True).start()
        if self.cfg.get("vod_folder") and Path(self.cfg["vod_folder"]).is_dir():
            self.folder_var.set(self.cfg["vod_folder"])
            self.root.after(200, self.scan_folder)

    # ----------------------------------------------------------------- theme --
    def _apply_theme(self, theme: str) -> None:
        theme = theme if theme in THEME_COLORS else "dark"
        self.colors = c = THEME_COLORS[theme]
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.root.configure(bg=c["bg"])
        style.configure(
            ".", background=c["bg"], foreground=c["fg"],
            bordercolor=c["border"], darkcolor=c["bg"], lightcolor=c["bg"],
            troughcolor=c["surface"], fieldbackground=c["field_bg"],
            focuscolor=c["accent"], selectbackground=c["accent"],
            selectforeground="#ffffff", insertcolor=c["fg"])
        style.configure("TLabel", background=c["bg"], foreground=c["fg"])
        style.configure("Muted.TLabel", foreground=c["muted"])
        style.configure("TFrame", background=c["bg"])
        style.configure("TLabelframe", background=c["bg"], bordercolor=c["border"],
                        relief="solid", borderwidth=1)
        style.configure("TLabelframe.Label", background=c["bg"], foreground=c["muted"])
        style.configure("TNotebook", background=c["bg"], borderwidth=0,
                        tabmargins=(10, 8, 10, 6))
        style.configure("TNotebook.Tab", padding=(16, 8), background=c["bg"],
                        borderwidth=0)
        # equal-size tabs; selection is marked purely by the accent color
        style.map("TNotebook.Tab",
                  background=[("selected", c["accent"]), ("active", c["hover"])],
                  foreground=[("selected", "#ffffff"), ("!selected", c["muted"])],
                  expand=[("selected", (0, 0, 0, 0))],
                  padding=[("selected", (16, 8))])
        style.configure("TButton", background=c["surface"], foreground=c["fg"],
                        borderwidth=1, relief="flat", padding=(10, 5))
        style.map("TButton",
                  background=[("disabled", c["bg"]), ("pressed", c["border"]),
                              ("active", c["hover"])],
                  foreground=[("disabled", c["muted"])])
        style.configure("Accent.TButton", background=c["accent"],
                        foreground="#ffffff", bordercolor=c["accent"])
        style.map("Accent.TButton",
                  background=[("disabled", c["surface"]),
                              ("pressed", c["accent_press"]),
                              ("active", c["accent_hover"])],
                  foreground=[("disabled", c["muted"])])
        style.configure("Treeview", background=c["field_bg"],
                        fieldbackground=c["field_bg"], foreground=c["fg"],
                        rowheight=26, borderwidth=0)
        style.map("Treeview", background=[("selected", c["accent"])],
                  foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background=c["surface"],
                        foreground=c["fg"], relief="flat", padding=(6, 4))
        style.map("Treeview.Heading", background=[("active", c["hover"])])
        for widget in ("TEntry", "TCombobox", "TSpinbox"):
            style.configure(widget, fieldbackground=c["field_bg"],
                            foreground=c["fg"], insertcolor=c["fg"],
                            bordercolor=c["border"], padding=4,
                            arrowcolor=c["fg"], background=c["surface"])
            style.map(widget,
                      fieldbackground=[("readonly", c["field_bg"]),
                                       ("disabled", c["bg"])],
                      foreground=[("disabled", c["muted"])])
        for widget in ("TCheckbutton", "TRadiobutton"):
            style.configure(widget, background=c["bg"], foreground=c["fg"],
                            indicatorbackground=c["field_bg"],
                            indicatorforeground=c["accent"])
            style.map(widget, background=[("active", c["bg"])])
        style.configure("Horizontal.TProgressbar", background=c["accent"],
                        troughcolor=c["surface"], borderwidth=0, thickness=10)
        for widget in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
            style.configure(widget, background=c["surface"], troughcolor=c["bg"],
                            bordercolor=c["bg"], arrowcolor=c["muted"], relief="flat")
            style.map(widget, background=[("active", c["border"])])
        style.configure("TSeparator", background=c["border"])
        # popup list of comboboxes (plain tk widgets, not ttk)
        self.root.option_add("*TCombobox*Listbox*Background", c["field_bg"])
        self.root.option_add("*TCombobox*Listbox*Foreground", c["fg"])
        self.root.option_add("*TCombobox*Listbox*selectBackground", c["accent"])
        self.root.option_add("*TCombobox*Listbox*selectForeground", "#ffffff")

        for tree in (self.video_tree, self.queue_tree, self.playlist_tree,
                     self.yt_tree):
            tree.tag_configure("uploaded", foreground=c["ok"])
            tree.tag_configure("done", foreground=c["ok"])
            tree.tag_configure("problem", foreground=c["err"])
            tree.tag_configure("error", foreground=c["err"])
            tree.tag_configure("uploading", foreground=c["info"])
            tree.tag_configure("odd_row", background=c["odd"])
        for txt in (self.desc_text, self.log_text, self.auto_log_text,
                    self.yt_desc_text, self.desc_template_text):
            txt.configure(bg=c["field_bg"], fg=c["fg"], insertbackground=c["fg"],
                          relief="flat", highlightthickness=0)
        self.yt_pl_list.configure(bg=c["field_bg"], fg=c["fg"], relief="flat",
                                  highlightthickness=0,
                                  selectbackground=c["accent"],
                                  selectforeground="#ffffff")
        self._set_titlebar_dark(theme == "dark")
        self._update_title_count()

    def _set_titlebar_dark(self, dark: bool) -> None:
        if sys.platform != "win32":
            return
        try:
            # Preferred: exact caption color matching the theme (Windows 11)
            import pywinstyles
            pywinstyles.change_header_color(self.root, self.colors["titlebar"])
            pywinstyles.change_title_color(self.root, self.colors["fg"])
        except Exception:
            try:
                from ctypes import byref, c_int, windll
                self.root.update_idletasks()
                hwnd = windll.user32.GetParent(self.root.winfo_id())
                # DWMWA_USE_IMMERSIVE_DARK_MODE (Windows 10 fallback)
                windll.dwmapi.DwmSetWindowAttribute(hwnd, 20,
                                                    byref(c_int(int(dark))), 4)
            except Exception:
                pass
        try:
            # nudge a repaint so the title bar updates immediately (skip while
            # the startup fade-in owns the alpha channel)
            if float(self.root.attributes("-alpha")) >= 0.99:
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
        self.notebook.add(tab, text=" 🎬 Videos ")

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
        ttk.Label(row2, text="Playlist:").pack(side="left", padx=(10, 2))
        self.bulk_playlist_var = tk.StringVar(value="(default)")
        self.bulk_playlist_combo = ttk.Combobox(
            row2, textvariable=self.bulk_playlist_var, width=22, state="readonly",
            values=["(default)", "(none)"])
        self.bulk_playlist_combo.pack(side="left")
        ttk.Button(row2, text="Apply",
                   command=self.bulk_apply_playlist).pack(side="left", padx=2)

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
        ttk.Label(row2, text="Playlist:").pack(side="left", padx=(10, 0))
        self.playlist_choice_var = tk.StringVar(value="(default)")
        self.editor_playlist_combo = ttk.Combobox(
            row2, textvariable=self.playlist_choice_var, width=22, state="readonly",
            values=["(default)", "(none)"])
        self.editor_playlist_combo.pack(side="left", padx=(4, 0))

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
        self.notebook.add(tab, text=" 📤 Queue & Progress ")

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
        ttk.Button(ctl, text="↻ Retry failed", command=self.retry_failed
                   ).pack(side="left", padx=6)
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

    # -------------------------------------------------------- automation tab --
    def _build_automation_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text=" 🤖 Automation ")

        box = ttk.LabelFrame(tab, text="Background folder watching")
        box.pack(fill="x", padx=10, pady=10)

        self.auto_enabled_var = tk.BooleanVar(value=bool(self.cfg.get("auto_scan", False)))
        ttk.Checkbutton(box, text="Watch the VOD folder and rescan automatically",
                        variable=self.auto_enabled_var,
                        command=self._automation_changed).pack(anchor="w", padx=8, pady=(8, 2))

        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=2)
        ttk.Label(row, text="Scan every").pack(side="left")
        self.auto_interval_var = tk.StringVar(
            value=str(self.cfg.get("auto_scan_interval_min", 10)))
        ttk.Spinbox(row, from_=1, to=1440, textvariable=self.auto_interval_var,
                    width=5, command=self._automation_changed).pack(side="left", padx=4)
        ttk.Label(row, text="minutes").pack(side="left")

        self.auto_queue_var = tk.BooleanVar(value=bool(self.cfg.get("auto_queue", True)))
        ttk.Checkbutton(box, text="Automatically queue new ready VODs "
                                  "(with generated metadata and the default playlist rule)",
                        variable=self.auto_queue_var, command=self._automation_changed
                        ).pack(anchor="w", padx=8, pady=2)
        self.auto_start_var = tk.BooleanVar(value=bool(self.cfg.get("auto_start", True)))
        ttk.Checkbutton(box, text="Automatically start uploading when the queue has items",
                        variable=self.auto_start_var, command=self._automation_changed
                        ).pack(anchor="w", padx=8, pady=2)
        self.auto_finalized_var = tk.BooleanVar(
            value=bool(self.cfg.get("auto_only_finalized", True)))
        ttk.Checkbutton(box, text="Skip raw .ts / not-finalized captures (recommended — "
                                  "they may still be recording)",
                        variable=self.auto_finalized_var, command=self._automation_changed
                        ).pack(anchor="w", padx=8, pady=(2, 8))

        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=(0, 8))
        self.auto_status_label = ttk.Label(row, text="Automation is off.")
        self.auto_status_label.pack(side="left")
        ttk.Button(row, text="Run a scan cycle now", command=self._auto_cycle
                   ).pack(side="right")

        ttk.Label(tab, text="Automation activity:").pack(anchor="w", padx=10)
        frame = ttk.Frame(tab)
        frame.pack(fill="both", expand=True, padx=10, pady=(2, 10))
        self.auto_log_text = tk.Text(frame, height=10, wrap="word", state="disabled")
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.auto_log_text.yview)
        self.auto_log_text.configure(yscrollcommand=sb.set)
        self.auto_log_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")

    def _automation_changed(self) -> None:
        self.cfg["auto_scan"] = bool(self.auto_enabled_var.get())
        try:
            self.cfg["auto_scan_interval_min"] = max(1, int(self.auto_interval_var.get()))
        except ValueError:
            pass
        self.cfg["auto_queue"] = bool(self.auto_queue_var.get())
        self.cfg["auto_start"] = bool(self.auto_start_var.get())
        self.cfg["auto_only_finalized"] = bool(self.auto_finalized_var.get())
        config.save_config(self.cfg)
        self._auto_countdown = self.cfg["auto_scan_interval_min"] * 60
        if self.cfg["auto_scan"]:
            self._auto_log("Automation enabled — scanning every "
                           f"{self.cfg['auto_scan_interval_min']} min.")
        else:
            self.auto_status_label.configure(text="Automation is off.")
            self._auto_log("Automation disabled.")

    def _cooldown_tick(self) -> None:
        """Show the active cooldown and auto-resume the queue when it ends."""
        if not self.cfg.get("cooldown_until"):
            return
        cooldown = limits.get_cooldown(self.cfg)
        idle = not (self.worker and self.worker.is_alive())
        if cooldown is None:                       # just expired
            limits.set_cooldown(self.cfg, None)
            config.save_config(self.cfg)
            if idle and any(i.status == "queued" for i in self.queue_items):
                if self.credentials is None:
                    self.credentials = auth.load_credentials()
                if self.credentials is not None:
                    self._log("Upload cooldown finished — resuming the queue.")
                    self.start_uploads()
        elif idle:
            self.current_label.configure(
                text=f"⏳ Cooldown until {limits.fmt_local(cooldown)} "
                     f"({self.cfg.get('cooldown_reason', '')}) — resumes automatically.")

    def _auto_tick(self) -> None:
        try:
            self._cooldown_tick()
            if self.cfg.get("auto_scan") and self.folder_var.get().strip():
                self._auto_countdown -= 1
                if self._auto_countdown <= 0:
                    self._auto_countdown = max(
                        60, int(self.cfg.get("auto_scan_interval_min", 10)) * 60)
                    self._auto_cycle()
                mins, secs = divmod(max(0, self._auto_countdown), 60)
                self.auto_status_label.configure(
                    text=f"Automation is ON — next scan in {mins}:{secs:02d}")
        finally:
            self.root.after(1000, self._auto_tick)

    def _auto_cycle(self) -> None:
        folder = self.folder_var.get().strip()
        if not folder:
            self._auto_log("No VOD folder configured — nothing to scan.")
            return
        before = set(self.vods)
        self.scan_folder()
        new = [k for k in self.vods if k not in before]
        if new:
            self._auto_log(f"Found {len(new)} new VOD folder(s).")
        queued = {i.key for i in self.queue_items
                  if i.status in ("queued", "uploading", "verifying", "done")}
        candidates = []
        for key, vod in self.vods.items():
            if key in self.registry or key in queued or vod.video_path is None:
                continue
            if self.cfg.get("auto_only_finalized", True) and vod.problems:
                continue
            candidates.append(key)
        if candidates and self.cfg.get("auto_queue", True):
            self._auto_log(f"Auto-queueing {len(candidates)} VOD(s).")
            self._enqueue(candidates)
        if self.cfg.get("auto_start", True) and \
                any(i.status == "queued" for i in self.queue_items) and \
                not (self.worker and self.worker.is_alive()):
            if limits.get_cooldown(self.cfg) is not None:
                self._auto_log("Queue has items, but an upload cooldown is active "
                               f"until {limits.fmt_local(limits.get_cooldown(self.cfg))}.")
                return
            if self.credentials is None:
                self.credentials = auth.load_credentials()
            if self.credentials is None:
                self._auto_log("Queue has items, but not signed in — cannot auto-start.")
            else:
                self._auto_log("Auto-starting uploads.")
                self.start_uploads()

    def _auto_log(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.auto_log_text.configure(state="normal")
        self.auto_log_text.insert("end", f"[{stamp}] {text}\n")
        self.auto_log_text.see("end")
        self.auto_log_text.configure(state="disabled")
        self._log(text)

    # ----------------------------------------------------------- manager tab --
    def _build_manager_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text=" 📺 My YouTube ")

        top = ttk.Frame(tab)
        top.pack(fill="x", padx=8, pady=8)
        ttk.Label(top, text="Channel:").pack(side="left")
        self.mgr_account_var = tk.StringVar()
        self.mgr_account_combo = ttk.Combobox(top, textvariable=self.mgr_account_var,
                                              width=32, state="readonly", values=[])
        self.mgr_account_combo.pack(side="left", padx=6)
        self.mgr_account_combo.bind("<<ComboboxSelected>>", self._on_account_selected)
        ttk.Button(top, text="⟳ Load videos", style="Accent.TButton",
                   command=self.load_yt_videos).pack(side="left", padx=6)
        self.mgr_count_label = ttk.Label(top, text="", style="Muted.TLabel")
        self.mgr_count_label.pack(side="left", padx=8)

        cols = ("check", "date", "title", "duration", "privacy", "views", "vstatus")
        self.yt_tree = ttk.Treeview(tab, columns=cols, show="headings",
                                    selectmode="extended", height=8)
        headings = {"check": (UNCHECKED, 36), "date": ("Published", 90),
                    "title": ("Title", 460), "duration": ("Length", 80),
                    "privacy": ("Privacy", 80), "views": ("Views", 80),
                    "vstatus": ("Status", 90)}
        for col, (text, width) in headings.items():
            self.yt_tree.heading(col, text=text)
            self.yt_tree.column(col, width=width,
                                anchor="center" if col not in ("title",) else "w")
        self.yt_tree.heading("check", command=self._toggle_all_yt)
        self.yt_checked: set[str] = set()
        self.yt_videos: list[dict] = []
        ysb = ttk.Scrollbar(tab, orient="vertical", command=self.yt_tree.yview)
        self.yt_tree.configure(yscrollcommand=ysb.set)
        self.yt_tree.pack(fill="both", expand=True, padx=(8, 0))
        ysb.place(in_=self.yt_tree, relx=1.0, rely=0, relheight=1.0, anchor="ne")
        self.yt_tree.bind("<Button-1>", self._on_yt_tree_click)
        self.yt_tree.bind("<Double-1>", lambda _e: self.yt_open_selected())
        self.yt_tree.bind("<<TreeviewSelect>>", self._on_yt_select)

        act = ttk.LabelFrame(tab, text="Actions (apply to checked videos)")
        act.pack(fill="x", padx=8, pady=8)
        row = ttk.Frame(act)
        row.pack(fill="x", padx=6, pady=6)
        ttk.Button(row, text="Check all", width=9,
                   command=lambda: self._set_all_yt_checked(True)).pack(side="left")
        ttk.Button(row, text="None", width=6,
                   command=lambda: self._set_all_yt_checked(False)).pack(
            side="left", padx=(4, 10))
        ttk.Label(row, text="Playlist:").pack(side="left")
        self.mgr_playlist_var = tk.StringVar()
        self.mgr_playlist_combo = ttk.Combobox(row, textvariable=self.mgr_playlist_var,
                                               width=24, state="readonly", values=[])
        self.mgr_playlist_combo.pack(side="left", padx=4)
        ttk.Button(row, text="Add to playlist", style="Accent.TButton",
                   command=self.yt_add_to_playlist).pack(side="left", padx=(0, 12))
        ttk.Label(row, text="Privacy:").pack(side="left")
        self.mgr_privacy_var = tk.StringVar(value="unlisted")
        ttk.Combobox(row, textvariable=self.mgr_privacy_var, width=9,
                     state="readonly", values=("private", "unlisted", "public")
                     ).pack(side="left", padx=4)
        ttk.Button(row, text="Set privacy",
                   command=self.yt_set_privacy).pack(side="left", padx=(0, 12))
        ttk.Button(row, text="Open in browser",
                   command=self.yt_open_selected).pack(side="left")
        ttk.Button(row, text="🗑 Delete from YouTube",
                   command=self.yt_delete).pack(side="right")

        # ---- full metadata editor for the selected video
        ed = ttk.LabelFrame(tab, text="Video editor (click a video above to load it)")
        ed.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.yt_edit_id: str | None = None
        self.yt_memberships: list[dict] = []

        row = ttk.Frame(ed)
        row.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(row, text="Title:").pack(side="left")
        self.yt_title_var = tk.StringVar()
        self.yt_title_var.trace_add(
            "write", lambda *_: self.yt_title_count.configure(
                text=f"{len(self.yt_title_var.get())}/100"))
        ttk.Entry(row, textvariable=self.yt_title_var).pack(
            side="left", fill="x", expand=True, padx=6)
        self.yt_title_count = ttk.Label(row, text="0/100", width=8)
        self.yt_title_count.pack(side="left")
        ttk.Button(row, text="💾 Save changes to YouTube", style="Accent.TButton",
                   command=self.yt_save_video).pack(side="left", padx=(6, 0))

        row = ttk.Frame(ed)
        row.pack(fill="x", padx=6, pady=2)
        ttk.Label(row, text="Tags:").pack(side="left")
        self.yt_tags_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.yt_tags_var).pack(
            side="left", fill="x", expand=True, padx=6)
        ttk.Label(row, text="Privacy:").pack(side="left")
        self.yt_edit_privacy_var = tk.StringVar(value="private")
        ttk.Combobox(row, textvariable=self.yt_edit_privacy_var, width=9,
                     state="readonly", values=("private", "unlisted", "public")
                     ).pack(side="left", padx=(4, 8))
        ttk.Label(row, text="Category:").pack(side="left")
        self.yt_category_var = tk.StringVar(value="Gaming (20)")
        self.yt_category_combo = ttk.Combobox(
            row, textvariable=self.yt_category_var, width=22, state="readonly",
            values=list(config.CATEGORIES))
        self.yt_category_combo.pack(side="left", padx=4)

        body = ttk.Frame(ed)
        body.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="Description:").pack(anchor="w")
        self.yt_desc_text = tk.Text(left, height=7, wrap="word", undo=True)
        self.yt_desc_text.pack(fill="both", expand=True, pady=(2, 0))

        right = ttk.Frame(body)
        right.pack(side="left", fill="y", padx=(10, 0))
        head = ttk.Frame(right)
        head.pack(fill="x")
        ttk.Label(head, text="In playlists:").pack(side="left")
        ttk.Button(head, text="⟳ Check", width=8,
                   command=self.yt_check_playlists).pack(side="right")
        self.yt_pl_list = tk.Listbox(right, height=5, width=34,
                                     activestyle="none", exportselection=False)
        self.yt_pl_list.pack(fill="y", expand=True, pady=2)
        row = ttk.Frame(right)
        row.pack(fill="x")
        ttk.Button(row, text="− Remove from playlist",
                   command=self.yt_remove_from_playlist).pack(fill="x")
        row = ttk.Frame(right)
        row.pack(fill="x", pady=(4, 0))
        self.yt_addpl_var = tk.StringVar()
        self.yt_addpl_combo = ttk.Combobox(row, textvariable=self.yt_addpl_var,
                                           width=24, state="readonly", values=[])
        self.yt_addpl_combo.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="＋ Add",
                   command=self.yt_add_one_to_playlist).pack(side="left", padx=(4, 0))

    def _on_yt_tree_click(self, event):
        if self.yt_tree.identify("region", event.x, event.y) == "cell" and \
                self.yt_tree.identify_column(event.x) == "#1":
            vid = self.yt_tree.identify_row(event.y)
            if vid:
                self.yt_checked.symmetric_difference_update({vid})
                self.yt_tree.set(vid, "check",
                                 CHECKED if vid in self.yt_checked else UNCHECKED)
            return "break"
        return None

    def _set_all_yt_checked(self, checked: bool) -> None:
        self.yt_checked = {v["id"] for v in self.yt_videos} if checked else set()
        for vid in self.yt_tree.get_children():
            self.yt_tree.set(vid, "check", CHECKED if checked else UNCHECKED)

    def _toggle_all_yt(self) -> None:
        self._set_all_yt_checked(len(self.yt_checked) < len(self.yt_videos))

    def _yt_checked_ids(self) -> list[str]:
        return [v for v in self.yt_tree.get_children() if v in self.yt_checked]

    def load_yt_videos(self) -> None:
        if self.credentials is None:
            messagebox.showwarning("My YouTube", "Not signed in — add an account "
                                   "in Settings first.")
            return
        creds = self.credentials
        self.mgr_count_label.configure(text="loading…")

        def worker():
            try:
                service = auth.build_service(creds)
                items = ytmanager.list_channel_videos(service)
                self.events.put({"type": "yt_videos", "items": items})
            except Exception as exc:
                self.events.put({"type": "log",
                                 "text": "Could not load channel videos: "
                                         + auth.describe_api_error(exc)})
                self.events.put({"type": "yt_videos", "items": None})

        threading.Thread(target=worker, daemon=True).start()

    def _yt_action(self, ids: list[str], fn, done_text: str,
                   reload_after: bool = False) -> None:
        """Run fn(service, video_id) over ids in a worker thread."""
        creds = self.credentials
        if creds is None:
            messagebox.showwarning("My YouTube", "Not signed in.")
            return

        def worker():
            try:
                service = auth.build_service(creds)
            except Exception as exc:
                self.events.put({"type": "log", "text": f"Action failed: {exc}"})
                return
            ok = 0
            for vid in ids:
                try:
                    fn(service, vid)
                    ok += 1
                except Exception as exc:
                    self.events.put({"type": "log",
                                     "text": f"{vid}: "
                                             + auth.describe_api_error(exc)})
            self.events.put({"type": "log", "text": done_text.format(n=ok)})
            if reload_after and ok:
                self.events.put({"type": "yt_reload"})

        threading.Thread(target=worker, daemon=True).start()

    # -------------------------------------------------- manager video editor --
    def _on_yt_select(self, _event=None) -> None:
        sel = self.yt_tree.selection()
        if len(sel) != 1 or sel[0] == self.yt_edit_id:
            return
        video_id = sel[0]
        if self.credentials is None:
            return
        creds = self.credentials

        def worker():
            try:
                video = ytmanager.get_video(auth.build_service(creds), video_id)
                self.events.put({"type": "yt_video_detail", "video": video})
            except Exception as exc:
                self.events.put({"type": "log",
                                 "text": "Could not load video details: "
                                         + auth.describe_api_error(exc)})

        threading.Thread(target=worker, daemon=True).start()

    def _load_yt_editor(self, video: dict) -> None:
        self.yt_edit_id = video["id"]
        self.yt_title_var.set(video["title"])
        self.yt_tags_var.set(", ".join(video["tags"]))
        self.yt_edit_privacy_var.set(video["privacy"])
        inv = {v: k for k, v in config.CATEGORIES.items()}
        label = inv.get(str(video["category_id"]),
                        f"Category {video['category_id']}")
        values = list(config.CATEGORIES)
        if label not in values:
            values.append(label)
        self.yt_category_combo.configure(values=values)
        self.yt_category_var.set(label)
        self.yt_desc_text.delete("1.0", "end")
        self.yt_desc_text.insert("1.0", video["description"])
        self.yt_memberships = []
        self.yt_pl_list.delete(0, "end")
        self.yt_pl_list.insert("end", "(press ⟳ Check)")

    def yt_save_video(self) -> None:
        if not self.yt_edit_id:
            messagebox.showinfo("My YouTube", "Select a video first.")
            return
        video_id = self.yt_edit_id
        title = scanner.sanitize_title(self.yt_title_var.get())
        description = self.yt_desc_text.get("1.0", "end-1c")[:4990]
        tags = [t.strip() for t in self.yt_tags_var.get().split(",") if t.strip()]
        privacy = self.yt_edit_privacy_var.get()
        category = config.CATEGORIES.get(self.yt_category_var.get())
        if category is None:   # dynamically added "Category NN" label
            digits = "".join(ch for ch in self.yt_category_var.get() if ch.isdigit())
            category = digits or "20"
        creds = self.credentials

        def worker():
            try:
                ytmanager.update_video(
                    auth.build_service(creds), video_id, title=title,
                    description=description, tags=tags,
                    category_id=category, privacy=privacy)
                self.events.put({"type": "log",
                                 "text": f"Saved changes to '{title}' on YouTube."})
                self.events.put({"type": "yt_row_update", "id": video_id,
                                 "title": title, "privacy": privacy})
            except Exception as exc:
                self.events.put({"type": "log",
                                 "text": "Saving to YouTube failed: "
                                         + auth.describe_api_error(exc)})

        threading.Thread(target=worker, daemon=True).start()

    def yt_check_playlists(self) -> None:
        if not self.yt_edit_id:
            return
        if not self.playlists:
            self.refresh_playlists()
            self._log("Playlists not loaded yet — refresh and press ⟳ Check again.")
            return
        video_id, creds = self.yt_edit_id, self.credentials
        channel_playlists = list(self.playlists)
        self.yt_pl_list.delete(0, "end")
        self.yt_pl_list.insert("end", "checking…")

        def worker():
            try:
                items = ytmanager.video_playlists(
                    auth.build_service(creds), channel_playlists, video_id)
                self.events.put({"type": "yt_memberships", "id": video_id,
                                 "items": items})
            except Exception as exc:
                self.events.put({"type": "log",
                                 "text": "Playlist check failed: "
                                         + auth.describe_api_error(exc)})

        threading.Thread(target=worker, daemon=True).start()

    def yt_remove_from_playlist(self) -> None:
        sel = self.yt_pl_list.curselection()
        if not sel or sel[0] >= len(self.yt_memberships):
            messagebox.showinfo("My YouTube", "Select a playlist in the list first "
                                "(press ⟳ Check to load them).")
            return
        membership = self.yt_memberships[sel[0]]
        creds = self.credentials

        def worker():
            try:
                ytmanager.remove_from_playlist(auth.build_service(creds),
                                               membership["item_id"])
                self.events.put({"type": "log",
                                 "text": f"Removed the video from playlist "
                                         f"'{membership['title']}'."})
                self.events.put({"type": "yt_recheck_playlists"})
            except Exception as exc:
                self.events.put({"type": "log",
                                 "text": "Remove failed: "
                                         + auth.describe_api_error(exc)})

        threading.Thread(target=worker, daemon=True).start()

    def yt_add_one_to_playlist(self) -> None:
        if not self.yt_edit_id:
            messagebox.showinfo("My YouTube", "Select a video first.")
            return
        title = self.yt_addpl_var.get()
        pid = self.playlist_ids.get(title)
        if not pid:
            messagebox.showinfo("My YouTube", "Pick a playlist first.")
            return
        video_id, creds = self.yt_edit_id, self.credentials

        def worker():
            try:
                playlists.add_to_playlist(auth.build_service(creds), pid, video_id)
                self.events.put({"type": "log",
                                 "text": f"Added the video to playlist '{title}'."})
                self.events.put({"type": "yt_recheck_playlists"})
            except Exception as exc:
                self.events.put({"type": "log",
                                 "text": "Add failed: "
                                         + auth.describe_api_error(exc)})

        threading.Thread(target=worker, daemon=True).start()

    def yt_add_to_playlist(self) -> None:
        ids = self._yt_checked_ids()
        title = self.mgr_playlist_var.get()
        pid = self.playlist_ids.get(title)
        if not ids or not pid:
            messagebox.showinfo("My YouTube", "Check some videos and pick a "
                                "playlist first (refresh playlists if the list "
                                "is empty).")
            return
        self._yt_action(ids,
                        lambda s, v: playlists.add_to_playlist(s, pid, v),
                        f"Added {{n}} video(s) to playlist '{title}'.")

    def yt_set_privacy(self) -> None:
        ids = self._yt_checked_ids()
        privacy = self.mgr_privacy_var.get()
        if not ids:
            messagebox.showinfo("My YouTube", "Check some videos first.")
            return
        self._yt_action(ids,
                        lambda s, v: ytmanager.set_privacy(s, v, privacy),
                        f"Set privacy '{privacy}' on {{n}} video(s).",
                        reload_after=True)

    def yt_open_selected(self) -> None:
        ids = self._yt_checked_ids() or list(self.yt_tree.selection())
        import webbrowser
        for vid in ids[:10]:
            webbrowser.open(f"https://youtu.be/{vid}")

    def yt_delete(self) -> None:
        ids = self._yt_checked_ids()
        if not ids:
            messagebox.showinfo("My YouTube", "Check some videos first.")
            return
        if not messagebox.askyesno(
                "Delete from YouTube",
                f"PERMANENTLY delete {len(ids)} video(s) from YouTube?\n\n"
                "This cannot be undone!", icon="warning"):
            return
        self._yt_action(ids, ytmanager.delete_video,
                        "Deleted {n} video(s) from YouTube.", reload_after=True)

    # ---------------------------------------------------------- settings tab --
    def _build_settings_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text=" ⚙ Settings ")

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
        row.pack(fill="x", padx=8, pady=2)
        ttk.Label(row, text="Active channel:").pack(side="left")
        self.account_var = tk.StringVar()
        self.account_combo = ttk.Combobox(row, textvariable=self.account_var,
                                          width=36, state="readonly", values=[])
        self.account_combo.pack(side="left", padx=6)
        self.account_combo.bind("<<ComboboxSelected>>", self._on_account_selected)

        row = ttk.Frame(acct)
        row.pack(fill="x", padx=8, pady=(4, 8))
        self.signin_btn = ttk.Button(row, text="➕ Add account…",
                                     style="Accent.TButton", command=self.sign_in)
        self.signin_btn.pack(side="left")
        ttk.Button(row, text="Remove account", command=self.sign_out
                   ).pack(side="left", padx=6)
        self.account_label = ttk.Label(row, text="Not signed in.")
        self.account_label.pack(side="left", padx=10)

        hint = ("Each added account is one YouTube channel (the browser sign-in page is "
                "where you pick the Google account and the brand channel). You can add "
                "several channels and switch between them here or in the My YouTube tab; "
                "uploads go to the active channel.")
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
        row.pack(fill="x", padx=8, pady=(6, 0))
        ttk.Label(row, text="Description template:").pack(side="left")
        ttk.Button(row, text="Reset to default", command=self._reset_desc_template
                   ).pack(side="right")
        self.desc_template_text = tk.Text(up, height=6, wrap="word", undo=True)
        self.desc_template_text.pack(fill="x", padx=8, pady=(2, 0))
        self.desc_template_text.insert(
            "1.0", self.cfg.get("description_template")
            or scanner.DEFAULT_DESCRIPTION_TEMPLATE)
        ttk.Label(up, text="Placeholders: {title} {streamer} {login} {date} {duration} "
                           "{game} {games} {chapters} (whole chapter block) {vod_id}. "
                           "Lines whose placeholders are all empty are dropped "
                           "automatically. Applies to newly scanned/reset videos.",
                  style="Muted.TLabel", wraplength=980,
                  justify="left").pack(anchor="w", padx=8, pady=(2, 0))

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
        ttk.Label(row, text="Max uploads per 24h:").pack(side="left", padx=(14, 0))
        self.daily_limit_var = tk.StringVar(
            value=str(self.cfg.get("daily_upload_limit", 0)))
        ttk.Entry(row, textvariable=self.daily_limit_var, width=5).pack(side="left", padx=4)
        ttk.Label(row, text="(0 = no limit; stops before YouTube errors)",
                  style="Muted.TLabel").pack(side="left")

        about = ttk.Frame(tab)
        about.pack(fill="x", padx=10, pady=(2, 0))
        ttk.Button(about, text="Save settings", command=self.save_settings
                   ).pack(side="right")
        ttk.Button(about, text="Check for updates",
                   command=lambda: threading.Thread(
                       target=self._update_check_bg, args=(True,),
                       daemon=True).start()).pack(side="left")
        ttk.Label(about, text=f"Version {__version__}", style="Muted.TLabel"
                  ).pack(side="left", padx=10)

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
        desc_tpl = (self.desc_template_text.get("1.0", "end-1c")
                    if hasattr(self, "desc_template_text")
                    else self.cfg.get("description_template"))
        return {
            "title": scanner.build_title(vod, self.template_var.get()
                                         if hasattr(self, "template_var")
                                         else self.cfg["title_template"]),
            "description": scanner.build_description(vod, desc_tpl or None),
            "tags": ", ".join(scanner.build_tags(vod)),
            "privacy": self.cfg["privacy"],
            "playlist_choice": "(default)",
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
        for i, vod in enumerate(self.vods.values()):
            status, tag = self._video_status(vod)
            dur = ""
            if vod.duration:
                h, rem = divmod(int(vod.duration), 3600)
                dur = f"{h}:{rem // 60:02d}:{rem % 60:02d}"
            tags = tuple(t for t in (tag, "odd_row" if i % 2 else None) if t)
            self.video_tree.insert(
                "", "end", iid=vod.key, tags=tags,
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

    def bulk_apply_playlist(self) -> None:
        choice = self.bulk_playlist_var.get() or "(default)"
        keys = self._checked_video_keys()
        for key in keys:
            self.metas[key]["playlist_choice"] = choice
        if self._editing_key in keys:
            self.playlist_choice_var.set(choice)
        self._log(f"Set playlist '{choice}' on {len(keys)} video(s).")

    # --------------------------------------------------------------- playlists --
    def _playlist_choices(self) -> list[str]:
        return ["(default)", "(none)"] + [p["title"] for p in self.playlists]

    def _update_playlist_choices(self) -> None:
        values = self._playlist_choices()
        titles = [p["title"] for p in self.playlists]
        self.editor_playlist_combo.configure(values=values)
        self.bulk_playlist_combo.configure(values=values)
        self.pl_fixed_combo.configure(values=titles)
        self.mgr_playlist_combo.configure(values=titles)
        self.yt_addpl_combo.configure(values=titles)
        if titles and self.mgr_playlist_var.get() not in titles:
            self.mgr_playlist_var.set(titles[0])
        if titles and self.yt_addpl_var.get() not in titles:
            self.yt_addpl_var.set(titles[0])

    def _resolve_playlist_spec(self, key: str) -> dict | None:
        """What playlist (if any) an enqueued video should end up in."""
        choice = self.metas.get(key, {}).get("playlist_choice", "(default)")
        if choice == "(none)":
            return None
        if choice != "(default)":
            pid = self.playlist_ids.get(choice)
            if pid:
                return {"type": "id", "value": pid, "title": choice}
            return {"type": "name", "value": choice}
        mode = self.cfg.get("playlist_mode", "none")
        if mode == "fixed" and self.cfg.get("playlist_fixed_id"):
            return {"type": "id", "value": self.cfg["playlist_fixed_id"],
                    "title": self.cfg.get("playlist_fixed_title", "")}
        if mode == "template":
            name = self._render_playlist_name(self.vods.get(key))
            if name:
                return {"type": "name", "value": name}
        return None

    def _render_playlist_name(self, vod: Vod | None) -> str:
        if vod is None:
            return ""
        template = self.cfg.get("playlist_template") or "{streamer} VODs"

        class _Safe(dict):
            def __missing__(self, k):
                return "{" + k + "}"

        values = _Safe(
            streamer=vod.streamer_name or vod.streamer_login,
            login=vod.streamer_login,
            game=vod.games[0] if vod.games else "",
            games=", ".join(vod.games),
            title=vod.stream_title,
            date=vod.date_str,
            year=vod.started_at.strftime("%Y") if vod.started_at else "",
            month=vod.started_at.strftime("%m") if vod.started_at else "",
        )
        try:
            return template.format_map(values).strip()[:150]
        except (ValueError, IndexError):
            return ""

    def _build_playlists_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text=" 📃 Playlists ")

        top = ttk.Frame(tab)
        top.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Button(top, text="⟳ Refresh playlists", command=self.refresh_playlists
                   ).pack(side="left")
        ttk.Label(top, text="New playlist:").pack(side="left", padx=(16, 2))
        self.new_pl_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.new_pl_var, width=32).pack(side="left", padx=4)
        self.new_pl_privacy = tk.StringVar(value="unlisted")
        ttk.Combobox(top, textvariable=self.new_pl_privacy, width=9, state="readonly",
                     values=("private", "unlisted", "public")).pack(side="left")
        ttk.Button(top, text="Create", style="Accent.TButton",
                   command=self.create_playlist_clicked).pack(side="left", padx=6)

        cols = ("title", "count", "privacy", "id")
        self.playlist_tree = ttk.Treeview(tab, columns=cols, show="headings", height=10)
        for col, (text, width, anchor) in {
                "title": ("Title", 400, "w"), "count": ("Videos", 70, "center"),
                "privacy": ("Privacy", 90, "center"),
                "id": ("Playlist ID", 320, "w")}.items():
            self.playlist_tree.heading(col, text=text)
            self.playlist_tree.column(col, width=width, anchor=anchor)
        self.playlist_tree.pack(fill="both", expand=True, padx=10, pady=4)
        self.playlist_tree.bind("<Double-1>", self._open_playlist_in_browser)
        ttk.Label(tab, text="Double-click a playlist to open it on YouTube.",
                  style="Muted.TLabel").pack(anchor="w", padx=10)

        rule = ttk.LabelFrame(
            tab, text="Default playlist for uploads (per-video override on the Videos tab)")
        rule.pack(fill="x", padx=10, pady=(4, 10))
        self.pl_mode_var = tk.StringVar(value=self.cfg.get("playlist_mode", "none"))
        row = ttk.Frame(rule)
        row.pack(fill="x", padx=8, pady=(6, 0))
        ttk.Radiobutton(row, text="Don't add uploads to any playlist",
                        variable=self.pl_mode_var, value="none",
                        command=self._playlist_rule_changed).pack(anchor="w")
        row = ttk.Frame(rule)
        row.pack(fill="x", padx=8)
        ttk.Radiobutton(row, text="Always add to:", variable=self.pl_mode_var,
                        value="fixed", command=self._playlist_rule_changed).pack(side="left")
        self.pl_fixed_var = tk.StringVar(value=self.cfg.get("playlist_fixed_title", ""))
        self.pl_fixed_combo = ttk.Combobox(row, textvariable=self.pl_fixed_var,
                                           width=36, state="readonly", values=[])
        self.pl_fixed_combo.pack(side="left", padx=6)
        self.pl_fixed_combo.bind("<<ComboboxSelected>>",
                                 lambda _e: self._playlist_rule_changed())
        row = ttk.Frame(rule)
        row.pack(fill="x", padx=8, pady=(0, 2))
        ttk.Radiobutton(row, text="Auto-create by name:", variable=self.pl_mode_var,
                        value="template", command=self._playlist_rule_changed
                        ).pack(side="left")
        self.pl_template_var = tk.StringVar(
            value=self.cfg.get("playlist_template", "{streamer} VODs {year}"))
        template_entry = ttk.Entry(row, textvariable=self.pl_template_var, width=42)
        template_entry.pack(side="left", padx=6)
        template_entry.bind("<FocusOut>", lambda _e: self._playlist_rule_changed())
        ttk.Label(rule,
                  text="Placeholders: {streamer} {login} {game} {games} {year} {month} "
                       "{date} {title}. Missing playlists are created on demand "
                       "(create = 50 quota units, adding a video = 50 units).",
                  style="Muted.TLabel", wraplength=1000, justify="left"
                  ).pack(anchor="w", padx=8, pady=(2, 8))

    def _open_playlist_in_browser(self, _event=None) -> None:
        sel = self.playlist_tree.selection()
        if not sel:
            return
        values = self.playlist_tree.item(sel[0], "values")
        if len(values) >= 4 and values[3]:
            import webbrowser
            webbrowser.open(f"https://www.youtube.com/playlist?list={values[3]}")

    def _playlist_rule_changed(self) -> None:
        self.cfg["playlist_mode"] = self.pl_mode_var.get()
        title = self.pl_fixed_var.get()
        self.cfg["playlist_fixed_title"] = title
        if title in self.playlist_ids:
            self.cfg["playlist_fixed_id"] = self.playlist_ids[title]
        self.cfg["playlist_template"] = self.pl_template_var.get()
        config.save_config(self.cfg)

    def refresh_playlists(self) -> None:
        if self.credentials is None:
            self.credentials = auth.load_credentials()
        if self.credentials is None:
            messagebox.showwarning("Playlists", "Not signed in to YouTube.")
            return
        creds = self.credentials

        def worker():
            try:
                service = auth.build_service(creds)
                items = playlists.list_playlists(service)
                self.events.put({"type": "playlists", "items": items})
            except Exception as exc:
                self.events.put({"type": "log",
                                 "text": "Could not load playlists: "
                                         + auth.describe_api_error(exc)})

        threading.Thread(target=worker, daemon=True).start()

    def create_playlist_clicked(self) -> None:
        title = self.new_pl_var.get().strip()
        if not title:
            return
        if self.credentials is None:
            messagebox.showwarning("Playlists", "Not signed in to YouTube.")
            return
        creds, privacy = self.credentials, self.new_pl_privacy.get()
        self.new_pl_var.set("")

        def worker():
            try:
                service = auth.build_service(creds)
                playlists.create_playlist(service, title, privacy)
                self.events.put({"type": "log",
                                 "text": f"Created playlist '{title}' ({privacy})"})
                self.events.put({"type": "playlists",
                                 "items": playlists.list_playlists(service)})
            except Exception as exc:
                self.events.put({"type": "log",
                                 "text": "Could not create playlist: "
                                         + auth.describe_api_error(exc)})

        threading.Thread(target=worker, daemon=True).start()

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
        self.playlist_choice_var.set(meta.get("playlist_choice", "(default)"))
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
        meta["playlist_choice"] = self.playlist_choice_var.get() or "(default)"
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
                playlist=self._resolve_playlist_spec(key),
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
            tags = tuple(t for t in (tag, "odd_row" if idx % 2 == 0 else None) if t)
            self.queue_tree.insert(
                "", "end", iid=item.key, tags=tags,
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
            self.notebook.select(5)
            return
        cooldown = limits.get_cooldown(self.cfg)
        if cooldown is not None:
            if not messagebox.askyesno(
                    "Upload cooldown",
                    f"YouTube upload limit hit earlier "
                    f"({self.cfg.get('cooldown_reason') or 'limit'}).\n"
                    f"Cooldown until {limits.fmt_local(cooldown)} — uploads will "
                    "resume automatically then.\n\nStart anyway now?"):
                return
            limits.set_cooldown(self.cfg, None)
            config.save_config(self.cfg)
        self.worker = UploadWorker(
            self.credentials, self.queue_items, self.events,
            daily_limit=int(self.cfg.get("daily_upload_limit", 0) or 0),
            count_recent=lambda: limits.count_recent(self.registry))
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

    def retry_failed(self) -> None:
        count = 0
        for item in self.queue_items:
            if item.status in ("error", "cancelled"):
                item.status = "queued"
                item.detail = ""
                item.progress = 0.0
                count += 1
        if count:
            self._refresh_queue_tree()
            self._refresh_video_tree()
            self._log(f"Re-queued {count} failed upload(s). Press Start "
                      "(or let automation pick them up).")
        else:
            self._log("No failed uploads in the queue.")

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

    def _active_account_id(self) -> str | None:
        ids = [a["id"] for a in self.accounts]
        active = self.cfg.get("active_account")
        if active in ids:
            return active
        return ids[0] if ids else None

    def _populate_account_combos(self) -> None:
        titles = [a["title"] for a in self.accounts]
        active = self._active_account_id()
        current = next((a["title"] for a in self.accounts if a["id"] == active), "")
        for combo, var in ((getattr(self, "account_combo", None),
                            getattr(self, "account_var", None)),
                           (getattr(self, "mgr_account_combo", None),
                            getattr(self, "mgr_account_var", None))):
            if combo is not None:
                combo.configure(values=titles)
                var.set(current)

    def _on_account_selected(self, event) -> None:
        title = event.widget.get()
        account = next((a for a in self.accounts if a["title"] == title), None)
        if account:
            self.switch_account(account["id"])

    def switch_account(self, account_id: str) -> None:
        self.cfg["active_account"] = account_id
        config.save_config(self.cfg)
        self.playlists = []
        self.playlist_ids = {}
        self._log(f"Switching channel to account {account_id}…")

        def worker():
            creds = auth.load_account(account_id)
            if creds is None:
                self.events.put({"type": "auth_err", "quiet": True,
                                 "error": "That account's session expired — "
                                          "add it again via Settings."})
                return
            self.events.put(self._channel_lookup_event(creds))

        threading.Thread(target=worker, daemon=True).start()

    def _restore_session(self) -> None:
        account_id = self._active_account_id()
        if account_id is None:
            return
        creds = auth.load_account(account_id)
        if creds is None:
            self.events.put({"type": "auth_err", "quiet": True,
                             "error": "Saved session can't be reused (expired, or the "
                                      "app needs new permissions). "
                                      "Add the account again via Settings."})
            return
        self.events.put(self._channel_lookup_event(creds))

    def sign_out(self) -> None:
        account_id = self._active_account_id()
        if account_id is None:
            return
        title = next((a["title"] for a in self.accounts if a["id"] == account_id),
                     account_id)
        if not messagebox.askyesno("Remove account",
                                   f"Remove the saved sign-in for '{title}'?"):
            return
        auth.remove_account(account_id)
        self.accounts = auth.list_accounts()
        self.cfg["active_account"] = ""
        config.save_config(self.cfg)
        self.credentials = None
        self.channel = None
        self._populate_account_combos()
        self.account_label.configure(text="Not signed in.")
        self.status_channel.configure(text="Not signed in")
        self._log(f"Removed account '{title}'.")
        if self.accounts:
            self.switch_account(self.accounts[0]["id"])

    # -------------------------------------------------------------- updates --
    def _update_check_bg(self, manual: bool = False) -> None:
        try:
            info = updater.check_for_update()
        except Exception as exc:
            if manual:
                self.events.put({"type": "log", "text": f"Update check failed: {exc}"})
            return
        self.events.put({"type": "update", "info": info, "manual": manual})

    # ---------------------------------------------------------------- events --
    def _poll_events(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                self._handle_event(ev)
        except queue.Empty:
            pass
        self._animate_progress()
        self.root.after(POLL_MS, self._poll_events)

    def _animate_progress(self) -> None:
        """Move the progress bar smoothly between (sparse) chunk reports by
        extrapolating from the last known position and speed."""
        pa = self._prog_anim
        if not pa["active"]:
            return
        elapsed = time.monotonic() - pa["ts"]
        predicted = min(pa["pct"] + pa["pct_per_s"] * elapsed, 99.7)
        if predicted > pa["shown"]:
            pa["shown"] += (predicted - pa["shown"]) * 0.25
            self.progressbar.configure(value=pa["shown"])

    def _reset_progress_anim(self) -> None:
        self._prog_anim.update(active=False, pct=0.0, pct_per_s=0.0, shown=0.0)
        self.progressbar.configure(value=0)

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
                self._reset_progress_anim()
                self.current_label.configure(text="Idle.")
                mode = self.cfg.get("after_upload", "keep")
                if verified and mode != "keep":
                    if self._recycle_vod(item.key, mode):
                        self._refresh_video_tree()
            if item and ev["status"] == "uploading":
                self.current_label.configure(text=f"Uploading: {item.title}")
                self._reset_progress_anim()
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
                size = item.vod.size_bytes or 1
                self._prog_anim.update(
                    active=True, pct=ev["pct"], ts=time.monotonic(),
                    pct_per_s=(ev["speed_bps"] / size * 100.0) if ev["speed_bps"] else 0.0)
                self.current_label.configure(
                    text=f"Uploading: {item.title} — {ev['pct']:.1f}%"
                         + (f" @ {fmt_speed(ev['speed_bps'])}, ETA {fmt_eta(ev['eta_s'])}"
                            if ev["speed_bps"] else ""))
                self._update_queue_row(item)
        elif etype == "playlists":
            self.playlists = ev["items"]
            self.playlist_ids = {p["title"]: p["id"] for p in self.playlists}
            self.playlist_tree.delete(*self.playlist_tree.get_children())
            for i, p in enumerate(self.playlists):
                self.playlist_tree.insert(
                    "", "end", tags=("odd_row",) if i % 2 else (),
                    values=(p["title"], p["count"], p["privacy"], p["id"]))
            self._update_playlist_choices()
            self._log(f"Loaded {len(self.playlists)} playlist(s) from the channel.")
        elif etype == "item_detail":
            item = self._item_by_key(ev["key"])
            if item:
                item.detail = ev["detail"]
                self._refresh_queue_tree()
        elif etype == "worker_done":
            self.start_btn.configure(state="normal")
            self.pause_btn.configure(state="disabled")
            self.cancel_btn.configure(state="disabled")
            self._reset_progress_anim()
            reason = ev.get("reason")
            if reason == "quota":
                until = limits.quota_cooldown(ev.get("detail", ""))
                limits.set_cooldown(self.cfg, until, ev.get("detail", "limit"))
                config.save_config(self.cfg)
                self.current_label.configure(
                    text=f"⏳ Upload limit hit — cooling down until "
                         f"{limits.fmt_local(until)}, resumes automatically.")
                self._log(f"Cooldown until {limits.fmt_local(until)} "
                          f"({ev.get('detail', '')}). Uploads resume automatically.")
            elif reason == "daily_limit":
                until = limits.next_slot(self.registry,
                                         int(self.cfg.get("daily_upload_limit", 0) or 0))
                limits.set_cooldown(self.cfg, until, "daily upload limit")
                config.save_config(self.cfg)
                self.current_label.configure(
                    text=f"⏳ Daily limit reached — next upload at "
                         f"{limits.fmt_local(until)}, resumes automatically.")
                self._log(f"Daily limit reached; next slot at {limits.fmt_local(until)}.")
            else:
                self.current_label.configure(
                    text={"finished": "Queue finished.",
                          "paused": "Paused."}.get(reason, "Idle."))
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
                # persist under the channel id and make it the active account
                account_id = auth.save_account(self.credentials, self.channel)
                if account_id != "legacy":
                    auth.remove_account("legacy")
                self.cfg["active_account"] = account_id
                config.save_config(self.cfg)
                self.accounts = auth.list_accounts()
                self._populate_account_combos()
                self.account_label.configure(text=f"Signed in — channel: {name}")
                self.status_channel.configure(text=f"YouTube channel: {name}")
                self._log(f"Active channel: {name}")
                self.refresh_playlists()
        elif etype == "yt_videos":
            items = ev.get("items")
            self.yt_videos = items or []
            self.yt_checked &= {v["id"] for v in self.yt_videos}
            self.yt_tree.delete(*self.yt_tree.get_children())
            for i, v in enumerate(self.yt_videos):
                tag = ("problem" if v["upload_status"] in ("failed", "rejected")
                       else "uploaded" if v["privacy"] == "public" else "")
                tags = tuple(t for t in (tag, "odd_row" if i % 2 else None) if t)
                self.yt_tree.insert(
                    "", "end", iid=v["id"], tags=tags,
                    values=(CHECKED if v["id"] in self.yt_checked else UNCHECKED,
                            v["published"], v["title"], v["duration"],
                            v["privacy"], f"{v['views']:,}", v["upload_status"]))
            self.mgr_count_label.configure(
                text=f"{len(self.yt_videos)} video(s)" if items is not None else "")
        elif etype == "yt_reload":
            self.load_yt_videos()
        elif etype == "yt_video_detail":
            self._load_yt_editor(ev["video"])
        elif etype == "yt_memberships":
            if ev["id"] == self.yt_edit_id:
                self.yt_memberships = ev["items"]
                self.yt_pl_list.delete(0, "end")
                if not self.yt_memberships:
                    self.yt_pl_list.insert("end", "(not in any playlist)")
                for m in self.yt_memberships:
                    self.yt_pl_list.insert("end", m["title"])
        elif etype == "yt_recheck_playlists":
            self.yt_check_playlists()
        elif etype == "yt_row_update":
            if self.yt_tree.exists(ev["id"]):
                self.yt_tree.set(ev["id"], "title", ev["title"])
                self.yt_tree.set(ev["id"], "privacy", ev["privacy"])
            for v in self.yt_videos:
                if v["id"] == ev["id"]:
                    v["title"], v["privacy"] = ev["title"], ev["privacy"]
        elif etype == "update":
            self._handle_update_event(ev)
        elif etype == "update_ready":
            self._save_editor()
            self.save_settings(silent=True)
            self._log("Restarting to finish the update…")
            self.root.after(500, self.root.destroy)
        elif etype == "auth_err":
            self.signin_btn.configure(state="normal")
            self._log(f"Sign-in problem: {ev['error']}")
            if ev.get("quiet"):
                # startup restore failure: inform without an error popup
                self.account_label.configure(text="Sign in again (see log).")
                self.status_channel.configure(text="Sign in required")
            else:
                self.account_label.configure(text="Not signed in.")
                messagebox.showerror("Sign in failed", ev["error"])

    def _handle_update_event(self, ev: dict) -> None:
        info = ev.get("info")
        if info is None:
            if ev.get("manual"):
                messagebox.showinfo("Updates", "You're running the latest version.")
            return
        self._log(f"New version v{info['version']} is available.")
        if self.worker and self.worker.is_alive():
            self._log("Update postponed — uploads are running. Use Settings → "
                      "Check for updates later.")
            return
        if updater.can_self_update() and info.get("exe_url"):
            if not messagebox.askyesno(
                    "Update available",
                    f"Version {info['version']} is available "
                    f"(you have {__version__}).\n\nDownload and restart now?"):
                return
            self._log(f"Downloading v{info['version']}…")

            def worker():
                try:
                    last = [0]

                    def progress(pct):
                        if pct - last[0] >= 25:
                            last[0] = pct
                            self.events.put({"type": "log",
                                             "text": f"Update download: {pct:.0f}%"})

                    updater.apply_update(info["exe_url"], progress)
                    self.events.put({"type": "update_ready"})
                except Exception as exc:
                    self.events.put({"type": "log",
                                     "text": f"Update failed: {exc}"})

            threading.Thread(target=worker, daemon=True).start()
        else:
            messagebox.showinfo(
                "Update available",
                f"Version {info['version']} is available (you have {__version__}).\n"
                f"Download it from:\n{info['html_url']}")

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
    def _reset_desc_template(self) -> None:
        self.desc_template_text.delete("1.0", "end")
        self.desc_template_text.insert("1.0", scanner.DEFAULT_DESCRIPTION_TEMPLATE)
        self.cfg["description_template"] = ""
        config.save_config(self.cfg)
        self._log("Description template reset to the default format.")

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
        desc_tpl = self.desc_template_text.get("1.0", "end-1c").strip("\n")
        self.cfg["description_template"] = (
            "" if desc_tpl == scanner.DEFAULT_DESCRIPTION_TEMPLATE else desc_tpl)
        self.cfg["notify_subscribers"] = bool(self.notify_var.get())
        self.cfg["made_for_kids"] = bool(self.kids_var.get())
        self.cfg["after_upload"] = config.AFTER_UPLOAD_CHOICES.get(
            self.after_upload_var.get(), "keep")
        try:
            self.cfg["daily_upload_limit"] = max(0, int(self.daily_limit_var.get()))
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
    root.attributes("-alpha", 0.0)   # start invisible, fade in below
    App(root)

    def fade_in(step: int = 0) -> None:
        alpha = min(1.0, step / 12)
        try:
            root.attributes("-alpha", alpha)
        except tk.TclError:
            return
        if alpha < 1.0:
            root.after(16, fade_in, step + 1)

    fade_in()
    root.mainloop()
