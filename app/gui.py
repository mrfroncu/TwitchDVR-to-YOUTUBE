"""Tkinter GUI shell.

Fixed tabs: Queues (admin + shared log) / Playlists (account-scoped) /
My YouTube (account-scoped) / Settings (app-wide) / About.
Dynamic, per-queue tabs (Videos / Queue & Progress / Automation) are built
by gui_queue_panel.QueuePanel, one instance per configured queue — either
a single rebindable instance plus a queue selector ("switcher" layout) or
one instance per queue in its own tab ("tabs" layout); see Settings →
Queue layout. The actual scan/queue/upload/cooldown model lives in
app/queue_runtime.py, shared with the web/Docker version.
"""
from __future__ import annotations

import re
import sys
import threading
import queue
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import auth, config, limits, lsdvr, playlists, queue_runtime, scanner, updater, ytmanager
from .gui_common import CHECKED, UNCHECKED
from .gui_queue_panel import QueuePanel
from .version import __version__

try:
    from send2trash import send2trash
except ImportError:      # running from source without the dependency
    send2trash = None

_INLINE_MD_RE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`)")

# Fluent-inspired palettes applied to the native 'clam' theme. Everything is
# drawn by Tk primitives (no image sprites), so resizing stays fast.
THEME_COLORS = {
    "midnight": {
        "bg": "#16161d", "surface": "#1f1f2a", "field_bg": "#1c1c26",
        "border": "#2d2d3d", "hover": "#28283a",
        "fg": "#ececf4", "muted": "#8b93a7",
        "accent": "#6d5df6", "accent_hover": "#7f70ff", "accent_press": "#5a4ad0",
        "ok": "#4ade80", "err": "#f87171", "info": "#60a5fa",
        "odd": "#1a1a24", "titlebar": "#16161d",
    },
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


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = config.load_config()

        def _recycle_bin_delete(path: Path) -> bool:
            send2trash(str(path))
            return True

        self.manager = queue_runtime.QueueManager(
            cfg=self.cfg,
            recycle_bin_delete=_recycle_bin_delete if send2trash is not None else None)

        self.events: queue.Queue = queue.Queue()   # app-level only: auth/yt/update/lsdvr
        self._log_seq = 0
        self.panels: dict[str, QueuePanel] = {}
        self._queue_containers: dict[str, ttk.Frame] = {}   # tabs-layout outer frames
        self._queue_select_ids: list[str] = []
        self._switcher_panel: QueuePanel | None = None
        self._active_queue_id: str | None = None
        self._qdef_dialog: tk.Toplevel | None = None

        self.accounts: list[dict] = auth.list_accounts()
        self._browse_account_id: str = ""
        self._browse_credentials = None
        self._browse_channel: dict | None = None
        self.playlists: list[dict] = []
        self.playlist_ids: dict[str, str] = {}
        self._yt_loaded_once = False
        self._playlists_loaded_once = False

        root.title(f"TwitchDVR to YouTube Uploader  v{__version__}")
        ui_scale = float(self.cfg.get("ui_scale", 1.0) or 1.0)
        root.geometry(f"{round(1240 * ui_scale)}x{round(820 * ui_scale)}")
        root.minsize(round(1000 * ui_scale), round(640 * ui_scale))

        self.colors = THEME_COLORS.get(self.cfg.get("theme", "dark"),
                                       THEME_COLORS["dark"])
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=6, pady=(6, 0))

        self._build_queues_tab()
        self._build_queue_bar()
        self._build_playlists_tab()
        self._build_manager_tab()
        self._build_settings_tab()
        self._build_about_tab()
        self._build_status_bar()
        self._layout_queues()
        self._apply_theme(self.cfg.get("theme", "dark"))

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        root.after(POLL_MS, self._poll_events)
        root.after(1000, self._refresh_status_bar)

        auth.migrate_legacy_token()
        self.accounts = auth.list_accounts()
        if self.accounts:
            default_q = next(iter(self.manager.list_queues()), None)
            self._browse_account_id = (
                default_q.account_id if default_q and default_q.account_id
                else self.accounts[0]["id"])
        self._populate_browse_combos()
        self._populate_account_mgmt()
        if self._browse_account_id:
            threading.Thread(target=self._restore_browse_session, daemon=True).start()
        if self.cfg.get("auto_update_check", True):
            threading.Thread(target=self._update_check_bg, daemon=True).start()

    # ----------------------------------------------------------------- theme --
    @staticmethod
    def _font_family() -> str:
        if sys.platform == "win32":
            return "Segoe UI"
        if sys.platform == "darwin":
            return "Helvetica Neue"
        return "TkDefaultFont"

    def _apply_theme(self, theme: str) -> None:
        theme = theme if theme in THEME_COLORS else "dark"
        self.colors = c = THEME_COLORS[theme]
        modern = self.cfg.get("ui_style", "modern") != "classic"
        scale = float(self.cfg.get("ui_scale", 1.0) or 1.0)
        family = self._font_family()
        base_font = (family, round((10 if modern else 9) * scale))
        bold_font = (family + " Semibold" if sys.platform == "win32" else family,
                     round((10 if modern else 9) * scale))
        btn_pad = tuple(round(v * scale) for v in ((14, 8) if modern else (10, 5)))
        tab_pad = tuple(round(v * scale) for v in ((18, 10) if modern else (16, 8)))
        row_h = round((30 if modern else 26) * scale)
        field_pad = round((6 if modern else 4) * scale)
        if not hasattr(self, "_base_tk_scaling"):
            self._base_tk_scaling = self.root.tk.call("tk", "scaling")
        try:
            self.root.tk.call("tk", "scaling", self._base_tk_scaling * scale)
        except tk.TclError:
            pass
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.root.configure(bg=c["bg"])
        style.configure(
            ".", background=c["bg"], foreground=c["fg"], font=base_font,
            bordercolor=c["border"], darkcolor=c["bg"], lightcolor=c["bg"],
            troughcolor=c["surface"], fieldbackground=c["field_bg"],
            focuscolor=c["accent"], selectbackground=c["accent"],
            selectforeground="#ffffff", insertcolor=c["fg"])
        style.configure("TLabel", background=c["bg"], foreground=c["fg"],
                        font=base_font)
        style.configure("Muted.TLabel", foreground=c["muted"],
                        font=(family, 9 if modern else 8))
        style.configure("TFrame", background=c["bg"])
        style.configure("TLabelframe", background=c["bg"], bordercolor=c["border"],
                        relief="solid", borderwidth=1)
        style.configure("TLabelframe.Label", background=c["bg"],
                        foreground=c["muted"], font=bold_font)
        style.configure("TNotebook", background=c["bg"], borderwidth=0,
                        tabmargins=(10, 8, 10, 6))
        style.configure("TNotebook.Tab", padding=tab_pad, background=c["bg"],
                        borderwidth=0, font=base_font)
        style.map("TNotebook.Tab",
                  background=[("selected", c["accent"]), ("active", c["hover"])],
                  foreground=[("selected", "#ffffff"), ("!selected", c["muted"])],
                  expand=[("selected", (0, 0, 0, 0))],
                  padding=[("selected", tab_pad)])
        style.configure("TButton", background=c["surface"], foreground=c["fg"],
                        borderwidth=1, relief="flat", padding=btn_pad,
                        font=base_font)
        style.map("TButton",
                  background=[("disabled", c["bg"]), ("pressed", c["border"]),
                              ("active", c["hover"])],
                  foreground=[("disabled", c["muted"])])
        style.configure("Accent.TButton", background=c["accent"],
                        foreground="#ffffff", bordercolor=c["accent"],
                        font=bold_font)
        style.map("Accent.TButton",
                  background=[("disabled", c["surface"]),
                              ("pressed", c["accent_press"]),
                              ("active", c["accent_hover"])],
                  foreground=[("disabled", c["muted"])])
        style.configure("Treeview", background=c["field_bg"],
                        fieldbackground=c["field_bg"], foreground=c["fg"],
                        rowheight=row_h, borderwidth=0, font=base_font)
        style.map("Treeview", background=[("selected", c["accent"])],
                  foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background=c["surface"],
                        foreground=c["fg"], relief="flat", font=bold_font,
                        padding=(8, 6) if modern else (6, 4))
        style.map("Treeview.Heading", background=[("active", c["hover"])])
        for widget in ("TEntry", "TCombobox", "TSpinbox"):
            style.configure(widget, fieldbackground=c["field_bg"],
                            foreground=c["fg"], insertcolor=c["fg"],
                            bordercolor=c["border"], padding=field_pad,
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
        self.root.option_add("*TCombobox*Listbox*Background", c["field_bg"])
        self.root.option_add("*TCombobox*Listbox*Foreground", c["fg"])
        self.root.option_add("*TCombobox*Listbox*selectBackground", c["accent"])
        self.root.option_add("*TCombobox*Listbox*selectForeground", "#ffffff")

        for tree in (self.queues_tree, self.playlist_tree, self.yt_tree):
            tree.tag_configure("uploaded", foreground=c["ok"])
            tree.tag_configure("done", foreground=c["ok"])
            tree.tag_configure("problem", foreground=c["err"])
            tree.tag_configure("error", foreground=c["err"])
            tree.tag_configure("uploading", foreground=c["info"])
            tree.tag_configure("odd_row", background=c["odd"])
        for txt in (self.log_text, self.yt_desc_text, self.changelog_text):
            txt.configure(bg=c["field_bg"], fg=c["fg"], insertbackground=c["fg"],
                          relief="flat", highlightthickness=0)
        self.changelog_text.tag_configure("h2", foreground=c["accent"])
        self.changelog_text.tag_configure("h3", foreground=c["info"])
        self.yt_pl_list.configure(bg=c["field_bg"], fg=c["fg"], relief="flat",
                                  highlightthickness=0,
                                  selectbackground=c["accent"],
                                  selectforeground="#ffffff")
        for canvas in getattr(self, "_scroll_canvases", []):
            canvas.configure(bg=c["bg"])
        for panel in self.panels.values():
            panel.apply_theme(c)
        self._restyle_settings_nav()
        self._set_titlebar_dark(theme != "light")

    def _set_titlebar_dark(self, dark: bool) -> None:
        if sys.platform != "win32":
            return
        try:
            import pywinstyles
            pywinstyles.change_header_color(self.root, self.colors["titlebar"])
            pywinstyles.change_title_color(self.root, self.colors["fg"])
        except Exception:
            try:
                from ctypes import byref, c_int, windll
                self.root.update_idletasks()
                hwnd = windll.user32.GetParent(self.root.winfo_id())
                windll.dwmapi.DwmSetWindowAttribute(hwnd, 20,
                                                    byref(c_int(int(dark))), 4)
            except Exception:
                pass
        try:
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

    def _on_ui_style_change(self, _event=None) -> None:
        self.cfg["ui_style"] = self.ui_style_var.get()
        config.save_config(self.cfg)
        self._apply_theme(self.cfg.get("theme", "midnight"))

    def _on_ui_scale_change(self, _event=None) -> None:
        try:
            scale = int(self.ui_scale_var.get().rstrip("%")) / 100.0
        except ValueError:
            scale = 1.0
        self.cfg["ui_scale"] = scale
        config.save_config(self.cfg)
        self._apply_theme(self.cfg.get("theme", "midnight"))
        self.root.minsize(round(1000 * scale), round(640 * scale))

    def _on_tab_changed(self, _event=None) -> None:
        current = self.notebook.nametowidget(self.notebook.select())
        if current is getattr(self, "manager_tab_frame", None) and not self._yt_loaded_once:
            self.load_yt_videos(quiet=True)
        elif current is getattr(self, "playlists_tab_frame", None) \
                and not self._playlists_loaded_once:
            self.refresh_playlists(quiet=True)
        if self.cfg.get("ui_style", "modern") == "classic":
            return
        try:
            if float(self.root.attributes("-alpha")) < 0.99:
                return
            steps = [0.86, 0.92, 0.97, 1.0]

            def fade(i=0):
                if i < len(steps):
                    try:
                        self.root.attributes("-alpha", steps[i])
                        self.root.after(18, fade, i + 1)
                    except tk.TclError:
                        pass

            fade()
        except tk.TclError:
            pass

    # ------------------------------------------------------- checkbox columns --
    def _bind_checkbox_column(self, tree: ttk.Treeview, checked: set[str],
                              on_change=None) -> None:
        """Shared click / click-and-drag / spacebar toggling for the fake
        checkbox column ("#1"), reused by every tree across the app and by
        each QueuePanel. `checked` must be the actual set object the tree's
        rows read from — captured once and mutated in place from then on."""
        state = {"dragging": False, "target": False}

        def paint(key: str, want_checked: bool) -> None:
            if want_checked:
                checked.add(key)
            else:
                checked.discard(key)
            tree.set(key, "check", CHECKED if want_checked else UNCHECKED)
            if on_change:
                on_change()

        def on_press(event):
            if tree.identify("region", event.x, event.y) != "cell" or \
                    tree.identify_column(event.x) != "#1":
                return None
            key = tree.identify_row(event.y)
            if not key:
                return None
            state["target"] = key not in checked
            paint(key, state["target"])
            state["dragging"] = True
            return "break"

        def on_motion(event):
            if not state["dragging"]:
                return
            key = tree.identify_row(event.y)
            if key and (key in checked) != state["target"]:
                paint(key, state["target"])

        def on_release(_event):
            state["dragging"] = False

        def on_space(_event):
            key = tree.focus()
            if key:
                paint(key, key not in checked)
            return "break"

        tree.bind("<Button-1>", on_press)
        tree.bind("<B1-Motion>", on_motion)
        tree.bind("<ButtonRelease-1>", on_release)
        tree.bind("<space>", on_space)

    @staticmethod
    def _check_all_shortcut(setter):
        def handler(_event=None):
            setter(True)
            return "break"
        return handler

    # ------------------------------------------------------------ queues tab --
    def _build_queues_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text=" 🗂 Queues ")

        top = ttk.Frame(tab)
        top.pack(fill="x", padx=8, pady=8)
        ttk.Button(top, text="➕ Add queue", style="Accent.TButton",
                   command=lambda: self._open_queue_editor(None)).pack(side="left")
        ttk.Label(
            top, text="Each queue uploads one at a time overall — when the active "
                      "queue finishes, cools down (its channel's limit) or errors, "
                      "the next ready queue takes over automatically. A shared "
                      "YouTube API-quota cooldown (shown in the status bar) pauses "
                      "every queue at once.",
            style="Muted.TLabel", wraplength=760, justify="left"
        ).pack(side="left", padx=12)

        cols = ("name", "folder", "channel", "status", "pending", "enabled")
        self.queues_tree = ttk.Treeview(tab, columns=cols, show="headings",
                                        selectmode="browse", height=8)
        headings = {"name": ("Name", 150), "folder": ("Folder", 300),
                    "channel": ("Channel", 150), "status": ("Status", 170),
                    "pending": ("Pending", 70), "enabled": ("Enabled", 70)}
        for col, (text, width) in headings.items():
            self.queues_tree.heading(col, text=text)
            self.queues_tree.column(col, width=width,
                                    anchor="center" if col in ("pending", "enabled") else "w")
        self.queues_tree.pack(fill="x", padx=8, pady=(0, 4))
        self.queues_tree.bind("<Double-1>", lambda _e: self._edit_selected_queue())

        ctl = ttk.Frame(tab)
        ctl.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(ctl, text="✏ Edit", command=self._edit_selected_queue).pack(side="left")
        ttk.Button(ctl, text="🗑 Remove", command=self._remove_selected_queue
                   ).pack(side="left", padx=(6, 0))
        ttk.Button(ctl, text="▲", width=3, command=lambda: self._move_selected_queue(-1)
                   ).pack(side="left", padx=(12, 0))
        ttk.Button(ctl, text="▼", width=3, command=lambda: self._move_selected_queue(1)
                   ).pack(side="left", padx=(2, 0))

        ttk.Label(tab, text="📜 Log (shared by every queue):").pack(anchor="w", padx=8)
        log_frame = ttk.Frame(tab)
        log_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log_text = tk.Text(log_frame, height=10, wrap="word", state="disabled")
        lsb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lsb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        lsb.pack(side="left", fill="y")

        self.queues_tab_frame = tab

    def _refresh_queues_tree(self) -> None:
        selected = self.queues_tree.selection()
        self.queues_tree.delete(*self.queues_tree.get_children())
        for rt in self.manager.list_queues():
            pending = sum(1 for i in rt.queue_items if i.status == "queued")
            if rt.worker and rt.worker.is_alive():
                status = "uploading"
            elif not rt.enabled:
                status = "disabled"
            else:
                cd = limits.get_cooldown(rt._state)
                if cd is not None:
                    status = f"cooldown until {limits.fmt_local(cd)}"
                elif rt.credentials is None:
                    status = "not signed in"
                else:
                    status = "idle"
            channel = rt.channel["title"] if rt.channel else (
                "(session expired)" if rt.account_id else "(none)")
            self.queues_tree.insert(
                "", "end", iid=rt.id,
                values=(rt.name, rt.vod_folder, channel, status, pending,
                        "yes" if rt.enabled else "no"))
        for iid in selected:
            if self.queues_tree.exists(iid):
                self.queues_tree.selection_add(iid)

    def _edit_selected_queue(self) -> None:
        sel = self.queues_tree.selection()
        if sel:
            self._open_queue_editor(sel[0])

    def _remove_selected_queue(self) -> None:
        sel = self.queues_tree.selection()
        if not sel:
            messagebox.showinfo("Remove queue", "Select a queue first.")
            return
        rt = self.manager.runtimes.get(sel[0])
        if rt is None:
            return
        if not messagebox.askyesno(
                "Remove queue",
                f"Remove queue '{rt.name}'? Its pending items and settings are "
                "deleted (local video files and anything already on YouTube "
                "are not)."):
            return
        try:
            self.manager.remove_queue(rt.id)
        except RuntimeError as exc:
            messagebox.showwarning("Remove queue", str(exc))
            return
        self._layout_queues()
        self._refresh_queues_tree()

    def _move_selected_queue(self, delta: int) -> None:
        sel = self.queues_tree.selection()
        if not sel:
            return
        order = [rt.id for rt in self.manager.list_queues()]
        idx = order.index(sel[0])
        new = idx + delta
        if not (0 <= new < len(order)):
            return
        order[idx], order[new] = order[new], order[idx]
        self.manager.reorder_queues(order)
        self._refresh_queues_tree()
        self.queues_tree.selection_set(sel[0])
        self._layout_queues(preferred_id=self._active_queue_id)

    # --------------------------------------------------------- queue editor --
    def _open_queue_editor(self, queue_id: str | None) -> None:
        if self._qdef_dialog is not None:
            try:
                self._qdef_dialog.destroy()
            except tk.TclError:
                pass
            self._qdef_dialog = None
        rt = self.manager.runtimes.get(queue_id) if queue_id else None
        c = self.colors
        dlg = tk.Toplevel(self.root)
        self._qdef_dialog = dlg
        dlg.title("Edit queue" if rt else "Add queue")
        dlg.configure(bg=c["bg"])
        dlg.geometry("760x680")
        dlg.transient(self.root)
        dlg.grab_set()

        def def_setting(key, fallback):
            return rt.setting(key, fallback) if rt else self.cfg.get(key, fallback)

        btn_bar = ttk.Frame(dlg)
        btn_bar.pack(side="bottom", fill="x", padx=14, pady=10)
        body = self._make_scrollable(dlg, track=False)

        def section(text):
            ttk.Label(body, text=text, font=(self._font_family(), 12, "bold")
                      ).pack(anchor="w", padx=10, pady=(14, 4))

        def row(parent=body):
            r = ttk.Frame(parent)
            r.pack(fill="x", padx=10, pady=3)
            return r

        name_var = tk.StringVar(value=rt.name if rt else "")
        folder_var = tk.StringVar(value=rt.vod_folder if rt else "")
        enabled_var = tk.BooleanVar(value=rt.enabled if rt else True)
        acct_titles = [a["title"] for a in self.accounts]
        current_acct_title = next(
            (a["title"] for a in self.accounts
             if a["id"] == (rt.account_id if rt else "")), "")
        account_var = tk.StringVar(value=current_acct_title)

        r = row()
        ttk.Label(r, text="Name:", width=14).pack(side="left")
        ttk.Entry(r, textvariable=name_var).pack(side="left", fill="x", expand=True)
        r = row()
        ttk.Label(r, text="VOD folder:", width=14).pack(side="left")
        ttk.Entry(r, textvariable=folder_var).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(r, text="Browse…", command=lambda: (
            lambda f: folder_var.set(f) if f else None
        )(filedialog.askdirectory(initialdir=folder_var.get() or None, parent=dlg))
                   ).pack(side="left")
        r = row()
        ttk.Label(r, text="YouTube account:", width=14).pack(side="left")
        ttk.Combobox(r, textvariable=account_var, state="readonly",
                     values=acct_titles, width=30).pack(side="left")
        ttk.Checkbutton(r, text="Enabled", variable=enabled_var
                         ).pack(side="left", padx=16)

        section("⬆ Upload defaults")
        privacy_var = tk.StringVar(value=def_setting("privacy", "private"))
        inv_cat = {v: k for k, v in config.CATEGORIES.items()}
        category_var = tk.StringVar(
            value=inv_cat.get(str(def_setting("category_id", "20")), "Gaming (20)"))
        r = row()
        ttk.Label(r, text="Privacy:", width=14).pack(side="left")
        ttk.Combobox(r, textvariable=privacy_var, state="readonly", width=10,
                     values=("private", "unlisted", "public")).pack(side="left")
        ttk.Label(r, text="Category:").pack(side="left", padx=(14, 4))
        ttk.Combobox(r, textvariable=category_var, state="readonly", width=22,
                     values=list(config.CATEGORIES)).pack(side="left")

        title_tpl_var = tk.StringVar(value=def_setting("title_template",
                                                        config.DEFAULTS["title_template"]))
        r = row()
        ttk.Label(r, text="Title template:", width=14).pack(side="left")
        ttk.Entry(r, textvariable=title_tpl_var).pack(side="left", fill="x", expand=True)
        ttk.Label(body, text="Placeholders: {title} {streamer} {login} {date} {game} {games}",
                  style="Muted.TLabel").pack(anchor="w", padx=10)

        ttk.Label(body, text="Description template (empty = built-in default):"
                  ).pack(anchor="w", padx=10, pady=(8, 2))
        desc_frame = ttk.Frame(body)
        desc_frame.pack(fill="both", padx=10)
        desc_tpl_text = tk.Text(desc_frame, height=6, wrap="word")
        desc_tpl_text.pack(fill="both", expand=True)
        desc_tpl_value = def_setting("description_template", "") or scanner.DEFAULT_DESCRIPTION_TEMPLATE
        desc_tpl_text.insert("1.0", desc_tpl_value)
        desc_tpl_text.configure(bg=c["field_bg"], fg=c["fg"], insertbackground=c["fg"],
                                relief="flat", highlightthickness=0)

        notify_var = tk.BooleanVar(value=bool(def_setting("notify_subscribers", False)))
        kids_var = tk.BooleanVar(value=bool(def_setting("made_for_kids", False)))
        extra_tags_var = tk.StringVar(value=def_setting("extra_tags", ""))
        r = row()
        ttk.Checkbutton(r, text="Notify subscribers on upload", variable=notify_var
                         ).pack(side="left")
        ttk.Checkbutton(r, text="Mark as “made for kids”", variable=kids_var
                         ).pack(side="left", padx=14)
        r = row()
        ttk.Label(r, text="Extra tags:", width=14).pack(side="left")
        ttk.Entry(r, textvariable=extra_tags_var).pack(side="left", fill="x", expand=True)

        section("⏱ Rate limiting")
        daily_var = tk.StringVar(value=str(def_setting("daily_upload_limit", 0)))
        speed_var = tk.StringVar(value=str(def_setting("upload_speed_limit", 0)))
        cooldown_h_var = tk.StringVar(value=str(def_setting("cooldown_hours", 24.5)))
        verify_var = tk.BooleanVar(value=bool(def_setting("verify_uploads", True)))
        r = row()
        ttk.Label(r, text="Max uploads/24h:", width=14).pack(side="left")
        ttk.Entry(r, textvariable=daily_var, width=6).pack(side="left")
        ttk.Label(r, text="(0 = no limit)", style="Muted.TLabel").pack(side="left", padx=6)
        ttk.Label(r, text="Speed limit MB/s:").pack(side="left", padx=(14, 4))
        ttk.Entry(r, textvariable=speed_var, width=6).pack(side="left")
        ttk.Label(r, text="Cooldown hours:").pack(side="left", padx=(14, 4))
        ttk.Entry(r, textvariable=cooldown_h_var, width=6).pack(side="left")
        r = row()
        ttk.Checkbutton(r, text="Verify each upload on YouTube after it finishes",
                        variable=verify_var).pack(side="left")

        section("🧹 After a verified upload")
        inv_after = {v: k for k, v in config.AFTER_UPLOAD_CHOICES.items()}
        after_var = tk.StringVar(
            value=inv_after.get(def_setting("after_upload", "keep"), "Keep local files"))
        recycle_fallback_var = tk.BooleanVar(
            value=bool(def_setting("recycle_fallback_delete", False)))
        lsdvr_delete_var = tk.BooleanVar(value=bool(def_setting("lsdvr_delete", False)))
        r = row()
        ttk.Combobox(r, textvariable=after_var, state="readonly", width=34,
                     values=list(config.AFTER_UPLOAD_CHOICES)).pack(side="left")
        r = row()
        ttk.Checkbutton(
            r, text="⚠ If the Recycle Bin isn't available, delete permanently "
                    "instead of doing nothing", variable=recycle_fallback_var
        ).pack(anchor="w")
        r = row()
        ttk.Checkbutton(
            r, text="Delete through the LiveStreamDVR API instead (connection "
                    "is set up once in Settings)", variable=lsdvr_delete_var
        ).pack(anchor="w")

        section("🤖 Automation")
        ttk.Label(body, text="Watching the folder / auto-queue / auto-start are "
                             "edited from this queue's own Automation tab.",
                  style="Muted.TLabel").pack(anchor="w", padx=10)

        section("📃 Playlist rule")
        plmode_var = tk.StringVar(value=def_setting("playlist_mode", "none"))
        pl_fixed_var = tk.StringVar(value=def_setting("playlist_fixed_title", ""))
        pl_template_var = tk.StringVar(
            value=def_setting("playlist_template", "{streamer} VODs {year}"))
        ttk.Radiobutton(body, text="Don't add uploads to any playlist",
                        variable=plmode_var, value="none"
                        ).pack(anchor="w", padx=10)
        r = row()
        ttk.Radiobutton(r, text="Always add to:", variable=plmode_var, value="fixed"
                        ).pack(side="left")
        ttk.Entry(r, textvariable=pl_fixed_var, width=30).pack(side="left", padx=6)
        r = row()
        ttk.Radiobutton(r, text="Auto-create by name:", variable=plmode_var, value="template"
                        ).pack(side="left")
        ttk.Entry(r, textvariable=pl_template_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Label(body, text="Placeholders: {streamer} {login} {game} {games} "
                             "{year} {month} {date} {title}",
                  style="Muted.TLabel").pack(anchor="w", padx=10, pady=(2, 10))

        def do_save():
            settings = {
                "privacy": privacy_var.get(),
                "category_id": config.CATEGORIES.get(category_var.get(), "20"),
                "title_template": title_tpl_var.get() or config.DEFAULTS["title_template"],
                "notify_subscribers": bool(notify_var.get()),
                "made_for_kids": bool(kids_var.get()),
                "extra_tags": extra_tags_var.get().strip(),
                "after_upload": config.AFTER_UPLOAD_CHOICES.get(after_var.get(), "keep"),
                "recycle_fallback_delete": bool(recycle_fallback_var.get()),
                "lsdvr_delete": bool(lsdvr_delete_var.get()),
                "verify_uploads": bool(verify_var.get()),
                "playlist_mode": plmode_var.get(),
                "playlist_fixed_title": pl_fixed_var.get(),
                "playlist_template": pl_template_var.get(),
            }
            desc_tpl = desc_tpl_text.get("1.0", "end-1c").strip("\n")
            settings["description_template"] = (
                "" if desc_tpl == scanner.DEFAULT_DESCRIPTION_TEMPLATE else desc_tpl)
            try:
                settings["daily_upload_limit"] = max(0, int(daily_var.get()))
            except ValueError:
                pass
            try:
                settings["upload_speed_limit"] = max(
                    0.0, float(speed_var.get().replace(",", ".")))
            except ValueError:
                pass
            try:
                settings["cooldown_hours"] = max(
                    0.5, float(cooldown_h_var.get().replace(",", ".")))
            except ValueError:
                pass

            name = name_var.get().strip() or "Queue"
            folder = folder_var.get().strip()
            account_id = next((a["id"] for a in self.accounts
                               if a["title"] == account_var.get()), "")
            if rt is not None and pl_fixed_var.get() in rt.playlist_ids:
                settings["playlist_fixed_id"] = rt.playlist_ids[pl_fixed_var.get()]

            if rt is None:
                new_rt = self.manager.create_queue(name, folder, account_id)
                self.manager.update_queue(
                    new_rt.id, {**settings, "enabled": bool(enabled_var.get())})
                preferred = new_rt.id
            else:
                self.manager.update_queue(rt.id, {
                    "name": name, "vod_folder": folder, "account_id": account_id,
                    "enabled": bool(enabled_var.get()), **settings,
                })
                preferred = rt.id
            dlg.destroy()
            self._qdef_dialog = None
            self._layout_queues(preferred_id=preferred)
            self._refresh_queues_tree()

        def do_cancel():
            dlg.destroy()
            self._qdef_dialog = None

        def do_remove():
            if rt is None:
                return
            if not messagebox.askyesno("Remove queue", f"Remove queue '{rt.name}'?"):
                return
            dlg.destroy()
            self._qdef_dialog = None
            try:
                self.manager.remove_queue(rt.id)
            except RuntimeError as exc:
                messagebox.showwarning("Remove queue", str(exc))
                return
            self._layout_queues()
            self._refresh_queues_tree()

        ttk.Button(btn_bar, text="💾 Save", style="Accent.TButton",
                   command=do_save).pack(side="left")
        ttk.Button(btn_bar, text="Cancel", command=do_cancel).pack(side="left", padx=6)
        if rt is not None:
            ttk.Button(btn_bar, text="🗑 Remove queue", command=do_remove
                       ).pack(side="right")

    # ------------------------------------------------------- queue bar/layout --
    def _build_queue_bar(self) -> None:
        self.queue_bar = ttk.Frame(self.root)
        self.queue_bar.pack(fill="x", padx=8, pady=(6, 0), before=self.notebook)
        self.queue_select_label = ttk.Label(self.queue_bar, text="Queue:")
        self.queue_select_var = tk.StringVar()
        self.queue_select_combo = ttk.Combobox(
            self.queue_bar, textvariable=self.queue_select_var,
            state="readonly", width=28)
        self.queue_select_combo.bind("<<ComboboxSelected>>", self._on_queue_select_changed)
        self.queue_bar_status = ttk.Label(self.queue_bar, text="", style="Muted.TLabel")
        self._no_queues_label = ttk.Label(
            self.queue_bar, text="No queues yet — add one on the Queues tab →",
            style="Muted.TLabel")

    def _layout_queues(self, preferred_id: str | None = None) -> None:
        for panel in list(self.panels.values()):
            panel.destroy()
        self.panels.clear()
        for frame in list(self._queue_containers.values()):
            try:
                self.notebook.forget(frame)
            except tk.TclError:
                pass
            frame.destroy()
        self._queue_containers.clear()
        self._switcher_panel = None
        self._active_queue_id = None

        self.queue_select_label.pack_forget()
        self.queue_select_combo.pack_forget()
        self.queue_bar_status.pack_forget()
        self._no_queues_label.pack_forget()

        queues = self.manager.list_queues()
        if not queues:
            self._no_queues_label.pack(side="left", padx=4, pady=4)
            return

        layout = self.cfg.get("queue_ui_layout", "switcher")
        if layout == "tabs":
            for rt in queues:
                container = ttk.Frame(self.notebook)
                self.notebook.add(container, text=f" {rt.name} ")
                self._queue_containers[rt.id] = container
                panel = QueuePanel(self, rt)
                panel.build(container, as_subtabs=True)
                self.panels[rt.id] = panel
            preferred = preferred_id if preferred_id in self._queue_containers else queues[0].id
            self._active_queue_id = preferred
            self.notebook.select(self._queue_containers[preferred])
        else:
            self.queue_select_label.pack(side="left", padx=(4, 4))
            self.queue_select_combo.pack(side="left")
            self.queue_bar_status.pack(side="left", padx=10)
            self.queue_select_combo.configure(values=[rt.name for rt in queues])
            self._queue_select_ids = [rt.id for rt in queues]
            by_id = {rt.id: rt for rt in queues}
            first = by_id.get(preferred_id) or queues[0]
            self._active_queue_id = first.id
            self.queue_select_combo.current(self._queue_select_ids.index(first.id))
            panel = QueuePanel(self, first)
            panel.build(self.notebook, as_subtabs=False)
            self.panels[first.id] = panel
            self._switcher_panel = panel
        self._apply_theme_to_panels()

    def _apply_theme_to_panels(self) -> None:
        for panel in self.panels.values():
            panel.apply_theme(self.colors)

    def _on_queue_select_changed(self, _event=None) -> None:
        idx = self.queue_select_combo.current()
        if idx < 0 or idx >= len(self._queue_select_ids) or self._switcher_panel is None:
            return
        qid = self._queue_select_ids[idx]
        rt = self.manager.runtimes.get(qid)
        if rt is None:
            return
        self.panels.pop(self._active_queue_id, None)
        self._switcher_panel.rebind(rt)
        self._switcher_panel.apply_theme(self.colors)
        self.panels[qid] = self._switcher_panel
        self._active_queue_id = qid

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
        self.mgr_account_combo.bind("<<ComboboxSelected>>", self._on_browse_account_selected)
        ttk.Button(top, text="⟳ Load videos", style="Accent.TButton",
                   command=self.load_yt_videos).pack(side="left", padx=6)
        self.mgr_count_label = ttk.Label(top, text="", style="Muted.TLabel")
        self.mgr_count_label.pack(side="left", padx=8)

        cols = ("check", "date", "title", "duration", "privacy", "views", "vstatus")
        self.yt_tree = ttk.Treeview(tab, columns=cols, show="headings",
                                    selectmode="extended", height=8)
        headings = {"check": (UNCHECKED, 46), "date": ("Published", 90),
                    "title": ("Title", 460), "duration": ("Length", 80),
                    "privacy": ("Privacy", 80), "views": ("Views", 80),
                    "vstatus": ("Status", 90)}
        for col, (text, width) in headings.items():
            self.yt_tree.heading(col, text=text)
            self.yt_tree.column(col, width=width,
                                anchor="center" if col not in ("title",) else "w")
        self.yt_tree.heading("check", command=self._toggle_all_yt)
        for col in ("date", "title", "duration", "privacy", "views", "vstatus"):
            self.yt_tree.heading(col, command=lambda c=col: self._sort_yt(c))
        self.yt_checked: set[str] = set()
        self.yt_videos: list[dict] = []
        self._yt_sort = {"col": "", "rev": False}
        ysb = ttk.Scrollbar(tab, orient="vertical", command=self.yt_tree.yview)
        self.yt_tree.configure(yscrollcommand=ysb.set)
        self.yt_tree.pack(fill="both", expand=True, padx=(8, 0))
        ysb.place(in_=self.yt_tree, relx=1.0, rely=0, relheight=1.0, anchor="ne")
        self._bind_checkbox_column(self.yt_tree, self.yt_checked)
        self.yt_tree.bind("<Double-1>", self._on_yt_double)
        self.yt_tree.bind("<<TreeviewSelect>>", self._on_yt_select)
        check_all_yt = self._check_all_shortcut(self._set_all_yt_checked)
        self.yt_tree.bind("<Control-a>", check_all_yt)
        self.yt_tree.bind("<Command-a>", check_all_yt)

        act = ttk.LabelFrame(tab, text="🔧 Actions (apply to checked videos)")
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

        ed = ttk.LabelFrame(tab, text="📝 Video editor (click a video above to load it)")
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

        self.manager_tab_frame = tab

    def _on_yt_double(self, event) -> None:
        if self.yt_tree.identify("region", event.x, event.y) != "cell":
            return
        row = self.yt_tree.identify_row(event.y)
        if row:
            import webbrowser
            webbrowser.open(f"https://youtu.be/{row}")

    @staticmethod
    def _dur_seconds(text: str) -> int:
        parts = [p for p in (text or "").split(":") if p.strip().isdigit()]
        seconds = 0
        for p in parts:
            seconds = seconds * 60 + int(p)
        return seconds

    def _sort_yt(self, col: str) -> None:
        field = {"date": "published", "title": "title", "duration": "duration",
                 "privacy": "privacy", "views": "views",
                 "vstatus": "upload_status"}[col]
        reverse = self._yt_sort["col"] == col and not self._yt_sort["rev"]
        self._yt_sort = {"col": col, "rev": reverse}
        if field == "duration":
            key = lambda v: self._dur_seconds(v["duration"])   # noqa: E731
        elif field == "views":
            key = lambda v: v["views"]                          # noqa: E731
        else:
            key = lambda v: str(v[field]).lower()               # noqa: E731
        self.yt_videos.sort(key=key, reverse=reverse)
        self._render_yt_table()

    def _render_yt_table(self) -> None:
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

    def _set_all_yt_checked(self, checked: bool) -> None:
        self.yt_checked.clear()
        if checked:
            self.yt_checked.update(v["id"] for v in self.yt_videos)
        for vid in self.yt_tree.get_children():
            self.yt_tree.set(vid, "check", CHECKED if checked else UNCHECKED)

    def _toggle_all_yt(self) -> None:
        self._set_all_yt_checked(len(self.yt_checked) < len(self.yt_videos))

    def _yt_checked_ids(self) -> list[str]:
        return [v for v in self.yt_tree.get_children() if v in self.yt_checked]

    def load_yt_videos(self, quiet: bool = False) -> None:
        if self._browse_credentials is None:
            if not quiet:
                messagebox.showwarning("My YouTube", "Not signed in — pick a channel "
                                       "with an account above, or add one in Settings.")
            return
        self._yt_loaded_once = True
        creds = self._browse_credentials
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
        creds = self._browse_credentials
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

    def _on_yt_select(self, _event=None) -> None:
        sel = self.yt_tree.selection()
        if len(sel) != 1 or sel[0] == self.yt_edit_id:
            return
        video_id = sel[0]
        if self._browse_credentials is None:
            return
        creds = self._browse_credentials

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
        label = inv.get(str(video["category_id"]), f"Category {video['category_id']}")
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
        if category is None:
            digits = "".join(ch for ch in self.yt_category_var.get() if ch.isdigit())
            category = digits or "20"
        creds = self._browse_credentials

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
        video_id, creds = self.yt_edit_id, self._browse_credentials
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
        creds = self._browse_credentials

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
        video_id, creds = self.yt_edit_id, self._browse_credentials

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

    # --------------------------------------------------------- playlists tab --
    def _build_playlists_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text=" 📃 Playlists ")

        top = ttk.Frame(tab)
        top.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(top, text="Channel:").pack(side="left")
        self.pl_account_var = tk.StringVar()
        self.pl_account_combo = ttk.Combobox(top, textvariable=self.pl_account_var,
                                             width=32, state="readonly", values=[])
        self.pl_account_combo.pack(side="left", padx=6)
        self.pl_account_combo.bind("<<ComboboxSelected>>", self._on_browse_account_selected)
        ttk.Button(top, text="⟳ Refresh playlists", command=self.refresh_playlists
                   ).pack(side="left", padx=(10, 0))
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
        ttk.Label(tab, text="Double-click a playlist to open it on YouTube. A "
                           "queue's own “always add uploads to playlist X” rule "
                           "is set in that queue's editor (Queues tab).",
                  style="Muted.TLabel", wraplength=900, justify="left"
                  ).pack(anchor="w", padx=10, pady=(4, 10))

        self.playlists_tab_frame = tab

    def _open_playlist_in_browser(self, _event=None) -> None:
        sel = self.playlist_tree.selection()
        if not sel:
            return
        values = self.playlist_tree.item(sel[0], "values")
        if len(values) >= 4 and values[3]:
            import webbrowser
            webbrowser.open(f"https://www.youtube.com/playlist?list={values[3]}")

    def refresh_playlists(self, quiet: bool = False) -> None:
        if self._browse_credentials is None:
            if not quiet:
                messagebox.showwarning("Playlists", "Not signed in to YouTube.")
            return
        self._playlists_loaded_once = True
        creds = self._browse_credentials

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
        if self._browse_credentials is None:
            messagebox.showwarning("Playlists", "Not signed in to YouTube.")
            return
        creds, privacy = self._browse_credentials, self.new_pl_privacy.get()
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

    # ---------------------------------------------------------- settings tab --
    def _make_scrollable(self, parent, track: bool = True) -> ttk.Frame:
        canvas = tk.Canvas(parent, highlightthickness=0, borderwidth=0)
        vsb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(inner_id, width=e.width))
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        def wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")

        inner.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", wheel))
        inner.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        if track:
            # Only persistent pages (e.g. About) register here for
            # _apply_theme to recolor later — a short-lived dialog's canvas
            # would be destroyed by the time the next theme change runs.
            self._scroll_canvases = getattr(self, "_scroll_canvases", [])
            self._scroll_canvases.append(canvas)
        return inner

    def _show_settings_page(self, key: str) -> None:
        for page in self._settings_pages.values():
            page.pack_forget()
        self._settings_pages[key].pack(fill="both", expand=True)
        self._settings_active_page = key
        self._restyle_settings_nav()

    def _restyle_settings_nav(self) -> None:
        c = self.colors
        active = getattr(self, "_settings_active_page", None)
        for key, lbl in self._settings_nav_buttons.items():
            selected = key == active
            lbl._selected = selected  # noqa: SLF001
            lbl.configure(bg=c["accent"] if selected else c["surface"],
                         fg="#ffffff" if selected else c["fg"])

    def _on_settings_nav_hover(self, key: str, entering: bool) -> None:
        lbl = self._settings_nav_buttons[key]
        if getattr(lbl, "_selected", False):
            return
        lbl.configure(bg=self.colors["hover"] if entering else self.colors["surface"])

    def _build_settings_tab(self) -> None:
        outer = ttk.Frame(self.notebook)
        self.notebook.add(outer, text=" ⚙ Settings ")

        container = ttk.Frame(outer)
        container.pack(fill="both", expand=True)
        nav = ttk.Frame(container, width=196)
        nav.pack(side="left", fill="y", padx=(10, 0), pady=10)
        nav.pack_propagate(False)
        content_host = ttk.Frame(container)
        content_host.pack(side="left", fill="both", expand=True, padx=14, pady=10)

        self._settings_pages: dict[str, ttk.Frame] = {}
        self._settings_nav_buttons: dict[str, tk.Label] = {}
        self._settings_active_page = None

        def add_page(key: str, icon: str, label: str) -> ttk.Frame:
            btn = tk.Label(
                nav, text=f"{icon}  {label}", anchor="w",
                padx=14, pady=9, font=(self._font_family(), 10),
                cursor="pointinghand" if sys.platform == "darwin" else "hand2")
            btn.pack(fill="x", pady=1)
            btn.bind("<Button-1>", lambda _e, k=key: self._show_settings_page(k))
            btn.bind("<Enter>", lambda _e, k=key: self._on_settings_nav_hover(k, True))
            btn.bind("<Leave>", lambda _e, k=key: self._on_settings_nav_hover(k, False))
            self._settings_nav_buttons[key] = btn
            page = ttk.Frame(content_host)
            self._settings_pages[key] = page
            ttk.Label(page, text=f"{icon} {label}",
                      font=(self._font_family(), 14, "bold")
                      ).pack(anchor="w", pady=(0, 12))
            return page

        # ------------------------------------------------------- appearance --
        looks = add_page("appearance", "🎨", "Appearance")
        row = ttk.Frame(looks)
        row.pack(fill="x", padx=8, pady=8)
        ttk.Label(row, text="Theme:").pack(side="left")
        self.theme_var = tk.StringVar(value=self.cfg.get("theme", "midnight"))
        theme_box = ttk.Combobox(row, textvariable=self.theme_var, width=10,
                                 state="readonly", values=("midnight", "dark", "light"))
        theme_box.pack(side="left", padx=6)
        theme_box.bind("<<ComboboxSelected>>", self._on_theme_change)
        ttk.Label(row, text="UI style:").pack(side="left", padx=(16, 0))
        self.ui_style_var = tk.StringVar(value=self.cfg.get("ui_style", "modern"))
        style_box = ttk.Combobox(row, textvariable=self.ui_style_var, width=8,
                                 state="readonly", values=("modern", "classic"))
        style_box.pack(side="left", padx=6)
        style_box.bind("<<ComboboxSelected>>", self._on_ui_style_change)
        ttk.Label(row, text="UI scale:").pack(side="left", padx=(16, 0))
        self.ui_scale_var = tk.StringVar(
            value=f"{round(float(self.cfg.get('ui_scale', 1.0)) * 100)}%")
        scale_box = ttk.Combobox(row, textvariable=self.ui_scale_var, width=6,
                                 state="readonly", values=("75%", "100%", "125%", "150%"))
        scale_box.pack(side="left", padx=6)
        scale_box.bind("<<ComboboxSelected>>", self._on_ui_scale_change)
        ttk.Label(row, text="Queue layout:").pack(side="left", padx=(16, 0))
        self.queue_layout_var = tk.StringVar(
            value=self.cfg.get("queue_ui_layout", "switcher"))
        layout_box = ttk.Combobox(row, textvariable=self.queue_layout_var, width=10,
                                  state="readonly", values=("switcher", "tabs"))
        layout_box.pack(side="left", padx=6)
        layout_box.bind("<<ComboboxSelected>>", self._on_queue_layout_change)
        ttk.Label(looks,
                  text="modern = larger type, roomier layout, animations · scale is "
                       "for small/high-DPI screens · queue layout picks between a "
                       "dropdown switcher and one tab per queue",
                  style="Muted.TLabel", wraplength=900
                  ).pack(anchor="w", padx=8, pady=(0, 8))

        # --------------------------------------------------- youtube accounts --
        acct = add_page("accounts", "🔑", "YouTube accounts")
        row = ttk.Frame(acct)
        row.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(row, text="OAuth client secret file:").pack(side="left")
        self.secret_var = tk.StringVar(value=self.cfg.get("client_secret_path", ""))
        secret_entry = ttk.Entry(row, textvariable=self.secret_var)
        secret_entry.pack(side="left", fill="x", expand=True, padx=6)
        secret_entry.bind("<FocusOut>", lambda _e: self.save_settings(silent=True))
        secret_entry.bind("<Return>", lambda _e: self.save_settings(silent=True))
        ttk.Button(row, text="Browse…", command=self._pick_secret).pack(side="left")

        row = ttk.Frame(acct)
        row.pack(fill="x", padx=8, pady=2)
        ttk.Label(row, text="Local redirect port (127.0.0.1):").pack(side="left")
        self.port_var = tk.StringVar(value=str(self.cfg.get("oauth_port", 8710)))
        port_entry = ttk.Entry(row, textvariable=self.port_var, width=8)
        port_entry.pack(side="left", padx=6)
        port_entry.bind("<FocusOut>", lambda _e: self.save_settings(silent=True))
        port_entry.bind("<Return>", lambda _e: self.save_settings(silent=True))
        ttk.Label(row, text="(OAuth client type must be “Desktop app”; any port works)"
                  ).pack(side="left")

        row = ttk.Frame(acct)
        row.pack(fill="x", padx=8, pady=(4, 8))
        self.signin_btn = ttk.Button(row, text="➕ Add account…",
                                     style="Accent.TButton", command=self.sign_in)
        self.signin_btn.pack(side="left")
        self.account_label = ttk.Label(row, text="")
        self.account_label.pack(side="left", padx=10)

        self.accounts_list_frame = ttk.Frame(acct)
        self.accounts_list_frame.pack(fill="x", padx=8, pady=(0, 8))

        hint = ("Each added account is one YouTube channel (the browser sign-in page "
                "is where you pick the Google account and the brand channel). Assign "
                "an account to a queue from the Queues tab, or pick one to browse with "
                "on the Playlists / My YouTube tabs.")
        ttk.Label(acct, text=hint, wraplength=900, style="Muted.TLabel"
                  ).pack(anchor="w", padx=8, pady=(0, 8))

        # --------------------------------------------------------- lsdvr page --
        lsd = add_page("lsdvr", "📡", "LiveStreamDVR")
        ttk.Label(
            lsd, text="One LiveStreamDVR connection, shared by every queue that "
                      "enables “Delete through the LiveStreamDVR API” in its own "
                      "queue editor. LiveStreamDVR watches its VOD folders, and "
                      "deleting one behind its back can crash it — with this "
                      "instead, the app asks LiveStreamDVR to delete the VOD "
                      "through its own API, so it stops watching it first.",
            wraplength=900, style="Muted.TLabel", justify="left"
        ).pack(anchor="w", padx=8, pady=(0, 10))

        row = ttk.Frame(lsd)
        row.pack(fill="x", padx=8, pady=2)
        ttk.Label(row, text="Address:").pack(side="left")
        self.lsdvr_url_var = tk.StringVar(value=self.cfg.get("lsdvr_url", ""))
        lsdvr_url_entry = ttk.Entry(row, textvariable=self.lsdvr_url_var, width=40)
        lsdvr_url_entry.pack(side="left", padx=6)
        lsdvr_url_entry.bind("<FocusOut>", lambda _e: self.save_settings(silent=True))
        lsdvr_url_entry.bind("<Return>", lambda _e: self.save_settings(silent=True))
        ttk.Label(row, text="e.g. http://192.168.1.10:8080", style="Muted.TLabel"
                  ).pack(side="left")

        row = ttk.Frame(lsd)
        row.pack(fill="x", padx=8, pady=2)
        ttk.Label(row, text="Password:").pack(side="left")
        self.lsdvr_password_var = tk.StringVar(value="")
        lsdvr_pass_entry = ttk.Entry(row, textvariable=self.lsdvr_password_var,
                                     width=24, show="*")
        lsdvr_pass_entry.pack(side="left", padx=6)
        lsdvr_pass_entry.bind("<FocusOut>", lambda _e: self._save_lsdvr_password())
        lsdvr_pass_entry.bind("<Return>", lambda _e: self._save_lsdvr_password())
        self.lsdvr_pass_hint = ttk.Label(
            row, text="(saved)" if self.cfg.get("lsdvr_password")
                      else "(empty if the panel has none)",
            style="Muted.TLabel")
        self.lsdvr_pass_hint.pack(side="left", padx=(4, 14))
        ttk.Button(row, text="Test connection", command=self._test_lsdvr
                   ).pack(side="left")
        self.lsdvr_status_label = ttk.Label(row, text="", style="Muted.TLabel")
        self.lsdvr_status_label.pack(side="left", padx=8)

        # ------------------------------------------------------- behavior page --
        beh = add_page("behavior", "⚙", "Behavior")
        row = ttk.Frame(beh)
        row.pack(fill="x", padx=8, pady=8)
        self.update_check_var = tk.BooleanVar(
            value=bool(self.cfg.get("auto_update_check", True)))
        ttk.Checkbutton(row, text="Check for updates on startup",
                        variable=self.update_check_var,
                        command=lambda: self.save_settings(silent=True)).pack(side="left")

        ttk.Separator(outer, orient="horizontal").pack(fill="x", padx=10, pady=(4, 0))
        about = ttk.Frame(outer)
        about.pack(fill="x", padx=14, pady=(8, 2))
        ttk.Button(about, text="Check for updates",
                   command=lambda: threading.Thread(
                       target=self._update_check_bg, args=(True,),
                       daemon=True).start()).pack(side="left")
        ttk.Label(about, text=f"Version {__version__}", style="Muted.TLabel"
                  ).pack(side="left", padx=10)
        ttk.Label(about, text="✓ Settings save automatically", style="Muted.TLabel"
                  ).pack(side="right")

        notes = (
            "Quota note: every upload costs 1600 API units and Google's default daily "
            "quota is 10,000 units — about 6 uploads per day, shared by every queue. "
            "A queue's own channel limit pauses only that queue; the shared quota "
            "pauses all of them. Both reset automatically.\n"
            "Important: while your Google Cloud OAuth app is unverified / in testing "
            "mode, videos uploaded through the API are locked to PRIVATE by YouTube.")
        ttk.Label(outer, text=notes, wraplength=1000, style="Muted.TLabel", justify="left"
                  ).pack(anchor="w", padx=14, pady=(0, 10))

        self._show_settings_page("appearance")

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
        self.cfg["lsdvr_url"] = self.lsdvr_url_var.get().strip()
        self.cfg["auto_update_check"] = bool(self.update_check_var.get())
        config.save_config(self.cfg)
        if not silent:
            self._log("Settings saved.")

    def _on_queue_layout_change(self, _event=None) -> None:
        self.cfg["queue_ui_layout"] = self.queue_layout_var.get()
        config.save_config(self.cfg)
        self._layout_queues(preferred_id=self._active_queue_id)

    def _save_lsdvr_password(self) -> None:
        pw = self.lsdvr_password_var.get()
        if not pw:
            return
        self.cfg["lsdvr_password"] = pw
        config.save_config(self.cfg)
        self.lsdvr_password_var.set("")
        self.lsdvr_pass_hint.configure(text="(saved)")
        self._log("LiveStreamDVR password saved.")

    def _test_lsdvr(self) -> None:
        url = self.lsdvr_url_var.get().strip()
        password = self.lsdvr_password_var.get() or self.cfg.get("lsdvr_password", "")
        self.lsdvr_status_label.configure(text="testing…")

        def worker():
            try:
                message = lsdvr.Client(url, password).check()
                self.events.put({"type": "lsdvr_test_result", "ok": True,
                                 "message": message})
            except lsdvr.LsdvrError as exc:
                self.events.put({"type": "lsdvr_test_result", "ok": False,
                                 "message": str(exc)})

        threading.Thread(target=worker, daemon=True).start()

    # ---------------------------------------------------------- accounts/auth --
    def _populate_browse_combos(self) -> None:
        titles = [a["title"] for a in self.accounts]
        current = next((a["title"] for a in self.accounts
                        if a["id"] == self._browse_account_id), "")
        for combo, var in ((self.mgr_account_combo, self.mgr_account_var),
                           (self.pl_account_combo, self.pl_account_var)):
            combo.configure(values=titles)
            var.set(current)

    def _populate_account_mgmt(self) -> None:
        for child in self.accounts_list_frame.winfo_children():
            child.destroy()
        for acc in self.accounts:
            row = ttk.Frame(self.accounts_list_frame)
            row.pack(fill="x", pady=1)
            used_by = [rt.name for rt in self.manager.list_queues()
                      if rt.account_id == acc["id"]]
            text = acc["title"] + (f"  ·  used by: {', '.join(used_by)}" if used_by else "")
            ttk.Label(row, text=text).pack(side="left")
            ttk.Button(row, text="Remove", width=8,
                       command=lambda a=acc: self._remove_account(a)
                       ).pack(side="right")

    def _remove_account(self, account: dict) -> None:
        if not messagebox.askyesno(
                "Remove account",
                f"Remove the saved sign-in for '{account['title']}'? Queues using "
                "it will show “not signed in” until you pick another account."):
            return
        auth.remove_account(account["id"])
        self.accounts = auth.list_accounts()
        for rt in self.manager.list_queues():
            if rt.account_id == account["id"]:
                rt.forget_credentials()
        if self._browse_account_id == account["id"]:
            self._browse_account_id = self.accounts[0]["id"] if self.accounts else ""
            self._browse_credentials = None
            self._browse_channel = None
        self._populate_browse_combos()
        self._populate_account_mgmt()
        self._log(f"Removed account '{account['title']}'.")

    def _on_browse_account_selected(self, event) -> None:
        title = event.widget.get()
        account = next((a for a in self.accounts if a["title"] == title), None)
        if account:
            self._switch_browse_account(account["id"])

    def _switch_browse_account(self, account_id: str) -> None:
        self._browse_account_id = account_id
        self.playlists = []
        self.playlist_ids = {}
        self._yt_loaded_once = False
        self._playlists_loaded_once = False
        self._browse_credentials = None
        self._browse_channel = None
        self._populate_browse_combos()
        self._log(f"Switching browse channel to account {account_id}…")

        def worker():
            creds = auth.load_account(account_id)
            if creds is None:
                self.events.put({"type": "auth_err", "quiet": True,
                                 "error": "That account's session expired — "
                                          "add it again via Settings."})
                return
            self.events.put(self._channel_lookup_event(creds))

        threading.Thread(target=worker, daemon=True).start()

    def _restore_browse_session(self) -> None:
        creds = auth.load_account(self._browse_account_id)
        if creds is None:
            self.events.put({"type": "auth_err", "quiet": True,
                             "error": "Saved session can't be reused (expired, or "
                                      "the app needs new permissions). Add the "
                                      "account again via Settings."})
            return
        self.events.put(self._channel_lookup_event(creds))

    def _channel_lookup_event(self, creds) -> dict:
        channel = None
        channel_error = None
        try:
            channel = auth.fetch_channel(auth.build_service(creds))
        except Exception as exc:
            channel_error = auth.describe_api_error(exc)
        return {"type": "auth_ok", "creds": creds, "channel": channel,
                "channel_error": channel_error}

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

    # ------------------------------------------------------------- about tab --
    def _build_about_tab(self) -> None:
        outer = ttk.Frame(self.notebook)
        self.notebook.add(outer, text=" ℹ About ")
        tab = self._make_scrollable(outer)

        box = ttk.Frame(tab)
        box.pack(pady=(20, 0))
        try:
            img = tk.PhotoImage(file=str(_asset_path("icon-192.png")))
            self._about_logo = img.subsample(2, 2)
            ttk.Label(box, image=self._about_logo).pack(pady=(6, 12))
        except Exception:
            pass
        ttk.Label(box, text="TwitchDVR to YouTube",
                  font=("Segoe UI Semibold", 18)).pack()
        ttk.Label(box, text=f"Version {__version__}",
                  style="Muted.TLabel").pack(pady=(2, 14))
        ttk.Label(box, text="Automated YouTube uploads for LiveStreamDVR "
                            "Twitch recordings —\nmetadata, chapters, playlists, "
                            "automation and channel management.",
                  justify="center").pack()
        ttk.Label(box, text="Created by Froncu", style="Muted.TLabel"
                  ).pack(pady=(16, 2))
        row = ttk.Frame(box)
        row.pack(pady=10)
        import webbrowser
        repo = "https://github.com/mrfroncu/TwitchDVR-to-YOUTUBE"
        ttk.Button(row, text="🌐 GitHub repository", style="Accent.TButton",
                   command=lambda: webbrowser.open(repo)).pack(side="left", padx=4)
        ttk.Button(row, text="📦 Releases",
                   command=lambda: webbrowser.open(repo + "/releases")
                   ).pack(side="left", padx=4)
        ttk.Button(row, text="Check for updates",
                   command=lambda: threading.Thread(
                       target=self._update_check_bg, args=(True,),
                       daemon=True).start()).pack(side="left", padx=4)
        ttk.Label(box, text="MIT License — provided as is, without warranty.\n"
                            "YouTube is a trademark of Google LLC; this project "
                            "is not affiliated with Google or Twitch.",
                  style="Muted.TLabel", justify="center").pack(pady=(18, 0))

        ttk.Separator(tab, orient="horizontal").pack(fill="x", padx=20, pady=(24, 10))
        ttk.Label(tab, text="Release notes", font=("Segoe UI Semibold", 12)
                  ).pack(anchor="w", padx=20)
        notes_frame = ttk.Frame(tab)
        notes_frame.pack(fill="both", expand=True, padx=20, pady=(6, 20))
        self.changelog_text = tk.Text(notes_frame, height=18, wrap="word",
                                      state="disabled", borderwidth=0,
                                      highlightthickness=0)
        csb = ttk.Scrollbar(notes_frame, orient="vertical",
                           command=self.changelog_text.yview)
        self.changelog_text.configure(yscrollcommand=csb.set)
        self.changelog_text.pack(side="left", fill="both", expand=True)
        csb.pack(side="left", fill="y")
        self._load_changelog()

    def _load_changelog(self) -> None:
        try:
            text = _changelog_path().read_text(encoding="utf-8")
        except OSError:
            text = "_No bundled release notes found._"
        self._render_markdown(self.changelog_text, text)

    def _render_markdown(self, widget: tk.Text, md: str) -> None:
        family = self._font_family()
        c = self.colors
        widget.tag_configure("h2", font=(family, 15, "bold"), foreground=c["accent"],
                             spacing1=14, spacing3=6)
        widget.tag_configure("h3", font=(family, 12, "bold"), foreground=c["info"],
                             spacing1=10, spacing3=4)
        widget.tag_configure("bullet", lmargin1=18, lmargin2=34, spacing1=2, spacing3=2)
        widget.tag_configure("bold", font=(family, 10, "bold"))
        widget.tag_configure("code", font=("Courier New", 10))

        blocks: list[tuple[str, str]] = []
        for raw in md.splitlines():
            line = raw.rstrip()
            if line.startswith("## "):
                blocks.append(("h2", line[3:].strip()))
            elif line.startswith("### "):
                blocks.append(("h3", line[4:].strip()))
            elif line.startswith("- "):
                blocks.append(("bullet", line[2:].strip()))
            elif not line.strip():
                blocks.append(("blank", ""))
            elif blocks and blocks[-1][0] == "bullet":
                blocks[-1] = ("bullet", blocks[-1][1] + " " + line.strip())
            else:
                blocks.append(("text", line.strip()))

        widget.configure(state="normal")
        widget.delete("1.0", "end")
        for kind, text in blocks:
            if kind == "blank":
                continue
            if kind == "bullet":
                self._insert_inline(widget, "•  " + text, ("bullet",))
                widget.insert("end", "\n")
            elif kind in ("h2", "h3"):
                self._insert_inline(widget, text, (kind,))
                widget.insert("end", "\n")
            else:
                self._insert_inline(widget, text, ())
                widget.insert("end", "\n")
        widget.configure(state="disabled")

    @staticmethod
    def _insert_inline(widget: tk.Text, text: str, base_tags: tuple = ()) -> None:
        for part in _INLINE_MD_RE.split(text):
            if not part:
                continue
            if part.startswith("**") and part.endswith("**") and len(part) > 3:
                widget.insert("end", part[2:-2], base_tags + ("bold",))
            elif part.startswith("`") and part.endswith("`") and len(part) > 1:
                widget.insert("end", part[1:-1], base_tags + ("code",))
            else:
                widget.insert("end", part, base_tags)

    # ------------------------------------------------------------ status bar --
    def _build_status_bar(self) -> None:
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", side="bottom")
        ttk.Separator(bar, orient="horizontal").pack(fill="x")
        inner = ttk.Frame(bar)
        inner.pack(fill="x", padx=8, pady=3)
        self.status_quota = ttk.Label(inner, text="")
        self.status_quota.pack(side="left")
        self.status_queue = ttk.Label(inner, text="")
        self.status_queue.pack(side="right")

    def _refresh_status_bar(self) -> None:
        quota_cd = limits.get_quota_cooldown(self.manager.cfg)
        self.status_quota.configure(
            text=(f"⏳ Shared API-quota cooldown until {limits.fmt_local(quota_cd)}"
                 if quota_cd is not None else "API quota: OK"))
        queues = self.manager.list_queues()
        pending = sum(sum(1 for i in rt.queue_items if i.status == "queued")
                     for rt in queues)
        done = sum(sum(1 for i in rt.queue_items if i.status == "done")
                  for rt in queues)
        self.status_queue.configure(
            text=f"{len(queues)} queue(s) · {pending} pending, {done} done")
        if hasattr(self, "queues_tree"):
            self._refresh_queues_tree()
        self.root.after(1000, self._refresh_status_bar)

    # ---------------------------------------------------------------- events --
    def _poll_events(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                self._handle_event(ev)
        except queue.Empty:
            pass
        try:
            while True:
                ev = self.manager.ui_events.get_nowait()
                panel = self.panels.get(ev.get("queue_id"))
                if panel is not None:
                    panel.handle_event(ev)
        except queue.Empty:
            pass
        new_log = self.manager.events_since(self._log_seq)
        for e in new_log:
            self._log_seq = max(self._log_seq, e["seq"])
            self._log(e["text"])
        self.root.after(POLL_MS, self._poll_events)

    def _handle_event(self, ev: dict) -> None:
        etype = ev.get("type")
        if etype == "log":
            self._log(ev["text"])
        elif etype == "lsdvr_test_result":
            prefix = "✓ " if ev["ok"] else "✗ "
            self.lsdvr_status_label.configure(text=prefix + ev["message"])
        elif etype == "playlists":
            self.playlists = ev["items"]
            self.playlist_ids = {p["title"]: p["id"] for p in self.playlists}
            self.playlist_tree.delete(*self.playlist_tree.get_children())
            for i, p in enumerate(self.playlists):
                self.playlist_tree.insert(
                    "", "end", tags=("odd_row",) if i % 2 else (),
                    values=(p["title"], p["count"], p["privacy"], p["id"]))
            titles = [p["title"] for p in self.playlists]
            self.mgr_playlist_combo.configure(values=titles)
            self.yt_addpl_combo.configure(values=titles)
            if titles and self.mgr_playlist_var.get() not in titles:
                self.mgr_playlist_var.set(titles[0])
            if titles and self.yt_addpl_var.get() not in titles:
                self.yt_addpl_var.set(titles[0])
            self._log(f"Loaded {len(self.playlists)} playlist(s) from the channel.")
        elif etype == "yt_videos":
            items = ev.get("items")
            self.yt_videos = items or []
            self.yt_checked &= {v["id"] for v in self.yt_videos}
            self._render_yt_table()
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
        elif etype == "update_progress":
            if getattr(self, "_update_prog_bar", None) is not None:
                try:
                    self._update_prog_bar.configure(value=ev["pct"])
                except tk.TclError:
                    pass
        elif etype == "update_failed":
            if getattr(self, "_update_prog_win", None) is not None:
                try:
                    self._update_prog_win.destroy()
                except tk.TclError:
                    pass
                self._update_prog_win = None
            self._log(f"Update failed: {ev['error']}")
            messagebox.showerror("Update failed", ev["error"])
        elif etype == "update_ready":
            self._log("Restarting to finish the update…")
            self.root.after(500, self.root.destroy)
        elif etype == "auth_ok":
            self._browse_credentials = ev["creds"]
            self._browse_channel = ev.get("channel")
            self.signin_btn.configure(state="normal")
            if ev.get("channel_error"):
                self.account_label.configure(text="Signed in — channel lookup failed")
                self._log(f"Signed in, but the channel lookup failed: {ev['channel_error']}")
                messagebox.showwarning(
                    "Signed in, but…",
                    "Google sign-in succeeded, but the YouTube API call failed:\n\n"
                    + ev["channel_error"])
            else:
                name = self._browse_channel["title"] if self._browse_channel \
                    else "(no channel on this account)"
                account_id = auth.save_account(self._browse_credentials, self._browse_channel)
                if account_id != "legacy":
                    auth.remove_account("legacy")
                if account_id not in [a["id"] for a in self.accounts]:
                    self._browse_account_id = account_id
                self.accounts = auth.list_accounts()
                self._populate_browse_combos()
                self._populate_account_mgmt()
                self.account_label.configure(text=f"Browsing: {name}")
                self._log(f"Browsing channel: {name}")
                self.refresh_playlists(quiet=True)
                self.load_yt_videos(quiet=True)
        elif etype == "auth_err":
            self.signin_btn.configure(state="normal")
            self._log(f"Sign-in problem: {ev['error']}")
            if ev.get("quiet"):
                self.account_label.configure(text="Sign in again (see log).")
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
        if any(rt.worker and rt.worker.is_alive() for rt in self.manager.list_queues()):
            self._log("Update postponed — uploads are running. Use Settings → "
                      "Check for updates later.")
            return
        self._show_update_dialog(info)

    def _show_update_dialog(self, info: dict) -> None:
        c = self.colors
        dlg = tk.Toplevel(self.root)
        dlg.title("Update available")
        dlg.configure(bg=c["bg"])
        w, h = 560, 440
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 2
        dlg.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(dlg, text=f"Version {info['version']} is available",
                  font=("Segoe UI Semibold", 14)).pack(pady=(16, 2))
        ttk.Label(dlg, text=f"You are running v{__version__}",
                  style="Muted.TLabel").pack()
        notes = tk.Text(dlg, wrap="word", height=12, bg=c["field_bg"],
                        fg=c["fg"], relief="flat", highlightthickness=0,
                        padx=12, pady=10)
        notes.insert("1.0", info.get("notes") or "(no release notes)")
        notes.configure(state="disabled")
        notes.pack(fill="both", expand=True, padx=14, pady=10)

        btns = ttk.Frame(dlg)
        btns.pack(fill="x", padx=14, pady=(0, 14))
        import webbrowser
        ttk.Button(btns, text="Later", command=dlg.destroy
                   ).pack(side="right", padx=(6, 0))
        ttk.Button(btns, text="Open releases page",
                   command=lambda: webbrowser.open(info["html_url"])
                   ).pack(side="right")
        if updater.can_self_update() and info.get("asset_url"):
            ttk.Button(btns, text="⬇ Update and restart", style="Accent.TButton",
                       command=lambda: self._start_update_download(info, dlg)
                       ).pack(side="left")
        else:
            ttk.Label(btns, text="Automatic update isn't available for this "
                                 "build — download it manually.",
                      style="Muted.TLabel").pack(side="left")

    def _start_update_download(self, info: dict, dlg: tk.Toplevel) -> None:
        dlg.destroy()
        c = self.colors
        prog = tk.Toplevel(self.root)
        prog.title("Updating…")
        prog.configure(bg=c["bg"])
        prog.geometry("420x120")
        prog.transient(self.root)
        prog.protocol("WM_DELETE_WINDOW", lambda: None)
        ttk.Label(prog, text=f"Downloading v{info['version']}…"
                  ).pack(pady=(18, 8))
        bar = ttk.Progressbar(prog, maximum=100.0, length=360)
        bar.pack(padx=20)
        self._update_prog_win = prog
        self._update_prog_bar = bar
        self._log(f"Downloading update v{info['version']}…")

        def worker():
            try:
                updater.apply_update(
                    info, progress=lambda pct: self.events.put(
                        {"type": "update_progress", "pct": pct}))
                self.events.put({"type": "update_ready"})
            except Exception as exc:
                self.events.put({"type": "update_failed", "error": str(exc)})

        threading.Thread(target=worker, daemon=True).start()

    def _update_check_bg(self, manual: bool = False) -> None:
        try:
            info = updater.check_for_update()
        except Exception as exc:
            if manual:
                self.events.put({"type": "log", "text": f"Update check failed: {exc}"})
            return
        self.events.put({"type": "update", "info": info, "manual": manual})

    def _log(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {text}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        active_uploads = [rt for rt in self.manager.list_queues()
                         if rt.worker and rt.worker.is_alive()]
        if active_uploads:
            names = ", ".join(rt.name for rt in active_uploads)
            if not messagebox.askyesno(
                    "Quit", f"An upload is still running ({names}) — quit anyway?\n"
                    "The partial upload will be discarded."):
                return
            for rt in active_uploads:
                rt.cancel_current()
                rt.pause_uploads()
        for panel in self.panels.values():
            panel._save_editor()
        for rt in self.manager.list_queues():
            rt._save_queue_state()
        self.save_settings(silent=True)
        self.root.destroy()


def _bundle_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def _asset_path(name: str) -> Path:
    return _bundle_root() / "assets" / name


def _changelog_path() -> Path:
    return _bundle_root() / "CHANGELOG.md"


def _set_app_icon(root: tk.Tk) -> None:
    try:
        ico = _asset_path("icon.ico")
        if sys.platform == "win32" and ico.exists():
            root.iconbitmap(default=str(ico))
        png = _asset_path("icon-192.png")
        if png.exists():
            img = tk.PhotoImage(file=str(png))
            root.iconphoto(True, img)
            root._app_icon = img
    except Exception:
        pass


AGREEMENT_TEXT = """TwitchDVR to YouTube — Terms of Use

By using this software you agree to the following:

1. LICENSE — This software is released under the MIT License. It is
   provided "AS IS", without warranty of any kind. The author is not
   liable for any damage, data loss, or account issues resulting from
   its use.

2. YOUR RESPONSIBILITY — You are responsible for the content you
   upload. You must comply with the YouTube Terms of Service, the
   YouTube API Services Terms of Service, Google's Privacy Policy, and
   copyright law. Upload only content you have the rights to publish.

3. GOOGLE / YOUTUBE API — This application uses the YouTube Data API
   with OAuth credentials that YOU create in your own Google Cloud
   project. API quota, upload limits, and any restrictions on your
   Google account are between you and Google.

4. LOCAL FILE OPERATIONS — Optional cleanup features can move files to
   the Recycle Bin or (in server mode) delete them permanently. They
   only run after a verified upload, but you enable and use them at
   your own risk. Keep backups of anything irreplaceable.

5. DATA — The app stores its configuration, OAuth tokens, and upload
   history locally on your machine. Nothing is sent anywhere except to
   Google/YouTube APIs and (for update checks) to GitHub.

If you do not agree, click Decline and the application will close.
"""


def _ensure_agreement(root: tk.Tk) -> bool:
    marker = config.APP_DIR / ".accepted"
    if marker.exists():
        return True
    dlg = tk.Toplevel(root)
    dlg.title("TwitchDVR to YouTube — Terms of Use")
    dlg.configure(bg="#1f1f1f")
    w, h = 660, 560
    x = (dlg.winfo_screenwidth() - w) // 2
    y = (dlg.winfo_screenheight() - h) // 2
    dlg.geometry(f"{w}x{h}+{x}+{y}")
    dlg.grab_set()
    dlg.protocol("WM_DELETE_WINDOW", lambda: None)

    text = tk.Text(dlg, wrap="word", bg="#2a2a2a", fg="#f0f0f0",
                   relief="flat", padx=16, pady=14,
                   highlightthickness=0, font=("Segoe UI", 10))
    text.insert("1.0", AGREEMENT_TEXT)
    text.configure(state="disabled")
    text.pack(fill="both", expand=True, padx=14, pady=(14, 8))

    result = {"ok": False}

    def accept():
        config.APP_DIR.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            f"accepted={datetime.now(timezone.utc).isoformat()} "
            f"version={__version__}\n", encoding="utf-8")
        result["ok"] = True
        dlg.destroy()

    btns = tk.Frame(dlg, bg="#1f1f1f")
    btns.pack(fill="x", padx=14, pady=(0, 14))
    tk.Button(btns, text="Decline", command=dlg.destroy, bg="#2b2b2b",
              fg="#f0f0f0", activebackground="#383838", relief="flat",
              padx=18, pady=7, bd=0).pack(side="right", padx=(8, 0))
    tk.Button(btns, text="Accept", command=accept, bg="#0f6fc5", fg="#ffffff",
              activebackground="#1d80d8", relief="flat", padx=24, pady=7,
              bd=0).pack(side="right")
    root.wait_window(dlg)
    return result["ok"]


def _show_splash(root: tk.Tk, on_done) -> None:
    splash = tk.Toplevel(root)
    splash.overrideredirect(True)
    w, h = 430, 250
    x = (splash.winfo_screenwidth() - w) // 2
    y = (splash.winfo_screenheight() - h) // 2
    splash.geometry(f"{w}x{h}+{x}+{y}")
    splash.configure(bg="#16161d")
    splash.attributes("-topmost", True)
    splash.attributes("-alpha", 0.0)
    canvas = tk.Canvas(splash, width=w, height=h, bg="#16161d",
                       highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    try:
        logo = tk.PhotoImage(file=str(_asset_path("icon-192.png"))).subsample(2, 2)
        canvas.create_image(w // 2, 84, image=logo)
        splash._logo = logo
    except Exception:
        canvas.create_text(w // 2, 84, text="▶", fill="#6d5df6",
                           font=("Segoe UI", 46))
    canvas.create_text(w // 2, 162, text="TwitchDVR → YouTube",
                       fill="#f0f0f0", font=("Segoe UI Semibold", 15))
    canvas.create_text(w // 2, 186, text=f"v{__version__}",
                       fill="#8b93a7", font=("Segoe UI", 9))
    bar_y = 222
    canvas.create_rectangle(24, bar_y, w - 24, bar_y + 4,
                            fill="#23232e", width=0)
    bar = canvas.create_rectangle(0, bar_y, 0, bar_y + 4,
                                  fill="#6d5df6", width=0)
    state = {"step": 0}

    def tick():
        step = state["step"] = state["step"] + 1
        try:
            splash.attributes("-alpha", min(1.0, step / 9))
            span = (w - 48) * 0.38
            travel = (w - 48) + span
            pos = 24 - span + (step * 13) % travel
            canvas.coords(bar, max(24, pos), bar_y,
                          min(w - 24, pos + span), bar_y + 4)
        except tk.TclError:
            return
        if step < 80:
            splash.after(16, tick)
        else:
            fade_out()

    def fade_out(alpha: float = 1.0):
        try:
            if alpha <= 0:
                splash.destroy()
                on_done()
                return
            splash.attributes("-alpha", alpha)
            splash.after(24, fade_out, alpha - 0.12)
        except tk.TclError:
            on_done()

    tick()


def main() -> None:
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    _set_app_icon(root)
    root.withdraw()
    if not _ensure_agreement(root):
        root.destroy()
        return
    App(root)

    def fade_in(step: int = 0) -> None:
        alpha = min(1.0, step / 12)
        try:
            root.attributes("-alpha", alpha)
        except tk.TclError:
            return
        if alpha < 1.0:
            root.after(16, fade_in, step + 1)

    def boot():
        root.attributes("-alpha", 0.0)
        root.deiconify()
        fade_in()

    _show_splash(root, boot)
    root.mainloop()
