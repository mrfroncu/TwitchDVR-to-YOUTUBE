"""Per-queue desktop widgets: Videos / Queue & Progress / Automation.

One `QueuePanel` instance owns this trio of pages, bound to one
`app.queue_runtime.QueueRuntime`. `app/gui.py`'s `App` either keeps a
single rebindable `QueuePanel` plus a queue selector ("switcher" layout) or
one `QueuePanel` per queue, each inside its own notebook tab ("tabs"
layout) — see `QueueRuntime`/`QueueManager` in app/queue_runtime.py for the
actual scan/queue/upload/cooldown model this only displays and drives.
"""
from __future__ import annotations

import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import limits, scanner
from .gui_common import CHECKED, UNCHECKED, fmt_eta, fmt_size, fmt_speed
from .queue_runtime import QueueRuntime
from .scanner import Vod

try:
    from send2trash import send2trash
except ImportError:
    send2trash = None


class QueuePanel:
    def __init__(self, app, runtime: QueueRuntime):
        self.app = app
        self.runtime = runtime
        self._nb: ttk.Notebook | None = None
        self._insert_at: int | None = None
        self._editing_key: str | None = None
        self._video_search_after_id: str | None = None
        self._recycling = False
        self._op_was_active = False
        self._prog_anim = {"active": False, "pct": 0.0, "pct_per_s": 0.0,
                           "ts": 0.0, "shown": 0.0}

    # ------------------------------------------------------------- build --
    def build(self, parent, as_subtabs: bool, insert_at: int | None = None) -> None:
        """`insert_at`, switcher layout only: the fixed position (right
        after the "Queues" tab) to insert these 3 pages at on `parent`
        (the app's main notebook, which may already hold other fixed
        tabs) — inserting rather than appending keeps them from drifting
        to the end every time the panel is torn down and rebuilt. Ignored
        for `as_subtabs=True`, where `parent` is this panel's own empty
        container and plain append order is already correct."""
        if as_subtabs:
            self._nb = ttk.Notebook(parent)
            self._nb.pack(fill="both", expand=True)
            host = self._nb
            self._insert_at = None
        else:
            host = parent
            self._insert_at = insert_at
        self._build_videos_tab(host)
        self._build_queue_tab(host)
        self._build_automation_tab(host)
        self._full_refresh()
        self._tick()

    def _add_tab(self, host, tab, text: str) -> None:
        if self._insert_at is None:
            host.add(tab, text=text)
        else:
            host.insert(self._insert_at, tab, text=text)
            self._insert_at += 1

    def destroy(self) -> None:
        if self._nb is not None:
            self._nb.destroy()
        else:
            for frame in (self.videos_tab_frame, self.queue_tab_frame,
                         self.automation_tab_frame):
                frame.master.forget(frame)
                frame.destroy()

    def rebind(self, runtime: QueueRuntime) -> None:
        self.runtime = runtime
        self._editing_key = None
        self.video_checked = set()
        self.queue_checked = set()
        # avoid popping a "failed" warning for a different queue's leftover
        # result right after switching to it
        self._op_was_active = False
        self._full_refresh()

    def _full_refresh(self) -> None:
        self.folder_var.set(self.runtime.vod_folder)
        games = sorted({g for vod in self.runtime.vods.values() for g in vod.games})
        self.video_filter_game_combo.configure(values=["(all)"] + games)
        self._refresh_video_tree()
        self._refresh_queue_tree()
        self._refresh_scan_label()
        self._refresh_automation_fields()
        self._update_playlist_choices()
        self._on_video_select()
        self._update_start_buttons()

    def apply_theme(self, colors: dict) -> None:
        for tree in (self.video_tree, self.queue_tree):
            tree.tag_configure("uploaded", foreground=colors["ok"])
            tree.tag_configure("done", foreground=colors["ok"])
            tree.tag_configure("problem", foreground=colors["err"])
            tree.tag_configure("error", foreground=colors["err"])
            tree.tag_configure("uploading", foreground=colors["info"])
            tree.tag_configure("odd_row", background=colors["odd"])
        self.desc_text.configure(
            bg=colors["field_bg"], fg=colors["fg"],
            insertbackground=colors["fg"], relief="flat", highlightthickness=0)

    # ------------------------------------------------------------ periodic --
    def _tick(self) -> None:
        """Lightweight polling for state QueueRuntime mutates directly
        (scan_state/op_state) without a one-shot ui event — cheap to just
        re-read since this is all in-process, no serialization."""
        if self._nb is not None and not self._nb.winfo_exists():
            return
        self._animate_progress()
        self._refresh_op_label()
        self.app.root.after(300, self._tick)

    # ------------------------------------------------------------- videos --
    def _build_videos_tab(self, host) -> None:
        tab = ttk.Frame(host)
        self._add_tab(host, tab, " 🎬 Videos ")
        self.videos_tab_frame = tab

        top = ttk.Frame(tab)
        top.pack(fill="x", padx=8, pady=8)
        ttk.Label(top, text="VOD folder:").pack(side="left")
        self.folder_var = tk.StringVar(value=self.runtime.vod_folder)
        ttk.Entry(top, textvariable=self.folder_var).pack(
            side="left", fill="x", expand=True, padx=6)
        ttk.Button(top, text="Browse…", command=self.pick_folder).pack(side="left")
        self.scan_btn = ttk.Button(top, text="🔍 Scan", command=self.scan_folder)
        self.scan_btn.pack(side="left", padx=(6, 0))
        self.scan_status_label = ttk.Label(tab, text="", style="Muted.TLabel")
        self.scan_status_label.pack(anchor="w", padx=10, pady=(0, 2))

        filt = ttk.Frame(tab)
        filt.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(filt, text="🔎").pack(side="left")
        self.video_filter_text = tk.StringVar()
        self.video_filter_text.trace_add(
            "write", lambda *_: self._on_video_search_typed())
        self.video_search_entry = ttk.Entry(
            filt, textvariable=self.video_filter_text, width=20)
        self.video_search_entry.pack(side="left", padx=(2, 10))
        ttk.Label(filt, text="Status:").pack(side="left")
        self.video_filter_status = tk.StringVar(value="all")
        status_box = ttk.Combobox(
            filt, textvariable=self.video_filter_status, width=13, state="readonly",
            values=("all", "ready", "queued", "uploaded", "verified",
                    "failed", "not_finalized"))
        status_box.pack(side="left", padx=(4, 10))
        status_box.bind("<<ComboboxSelected>>", self._on_video_filter_changed)
        ttk.Label(filt, text="Game:").pack(side="left")
        self.video_filter_game = tk.StringVar(value="(all)")
        self.video_filter_game_combo = ttk.Combobox(
            filt, textvariable=self.video_filter_game, width=16, state="readonly",
            values=["(all)"])
        self.video_filter_game_combo.pack(side="left", padx=(4, 10))
        self.video_filter_game_combo.bind(
            "<<ComboboxSelected>>", self._on_video_filter_changed)
        ttk.Label(filt, text="From:").pack(side="left")
        self.video_filter_date_from = tk.StringVar()
        date_from_entry = ttk.Entry(
            filt, textvariable=self.video_filter_date_from, width=10)
        date_from_entry.pack(side="left", padx=(4, 0))
        date_from_entry.bind("<FocusOut>", self._on_video_filter_changed)
        date_from_entry.bind("<Return>", self._on_video_filter_changed)
        ttk.Button(filt, text="📅", width=3, command=lambda: self._open_calendar_picker(
            self.video_filter_date_from, date_from_entry)).pack(side="left", padx=(2, 6))
        ttk.Label(filt, text="To:").pack(side="left")
        self.video_filter_date_to = tk.StringVar()
        date_to_entry = ttk.Entry(
            filt, textvariable=self.video_filter_date_to, width=10)
        date_to_entry.pack(side="left", padx=(4, 0))
        date_to_entry.bind("<FocusOut>", self._on_video_filter_changed)
        date_to_entry.bind("<Return>", self._on_video_filter_changed)
        ttk.Button(filt, text="📅", width=3, command=lambda: self._open_calendar_picker(
            self.video_filter_date_to, date_to_entry)).pack(side="left", padx=(2, 10))
        ttk.Button(filt, text="✕ Clear filters",
                   command=self._clear_video_filters).pack(side="left")

        self.video_summary_label = ttk.Label(tab, text="", style="Muted.TLabel")
        self.video_summary_label.pack(anchor="w", padx=10, pady=(0, 4))

        cols = ("check", "date", "streamer", "title", "duration", "size",
                "chapters", "status")
        self.video_tree = ttk.Treeview(tab, columns=cols, show="headings",
                                       selectmode="extended", height=10)
        headings = {"check": (UNCHECKED, 46), "date": ("Date", 90),
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
        self.app._bind_checkbox_column(
            self.video_tree, self.video_checked,
            on_change=lambda: self._update_video_summary(self._filtered_vods()))
        self.video_tree.bind("<<TreeviewSelect>>", self._on_video_select)
        self.video_tree.bind("<Double-1>", self._open_vod_folder)
        check_all_videos = self.app._check_all_shortcut(self._set_all_videos_checked)
        self.video_tree.bind("<Control-a>", check_all_videos)
        self.video_tree.bind("<Command-a>", check_all_videos)   # macOS

        bulk = ttk.LabelFrame(tab, text="🔧 Bulk actions (apply to checked rows)")
        bulk.pack(fill="x", padx=8, pady=(6, 0))

        def vsep(parent):
            ttk.Separator(parent, orient="vertical").pack(
                side="left", fill="y", padx=10, pady=2)

        row = ttk.Frame(bulk)
        row.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Button(row, text="☑ All", width=7,
                   command=lambda: self._set_all_videos_checked(True)).pack(side="left")
        ttk.Button(row, text="☐ None", width=8,
                   command=lambda: self._set_all_videos_checked(False)).pack(
            side="left", padx=(4, 0))
        vsep(row)
        ttk.Button(row, text="➕ Add to queue", style="Accent.TButton",
                   command=self.bulk_add_checked).pack(side="left")
        ttk.Button(row, text="♻ Reset metadata",
                   command=self.bulk_reset_meta).pack(side="left", padx=(6, 0))
        vsep(row)
        ttk.Label(row, text="🔒 Privacy").pack(side="left", padx=(0, 4))
        self.bulk_privacy_var = tk.StringVar(value=self.runtime.setting("privacy"))
        ttk.Combobox(row, textvariable=self.bulk_privacy_var, width=9, state="readonly",
                     values=("private", "unlisted", "public")).pack(side="left")
        ttk.Button(row, text="Set",
                   command=self.bulk_apply_privacy).pack(side="left", padx=(4, 0))
        ttk.Label(row, text="📃 Playlist").pack(side="left", padx=(12, 4))
        self.bulk_playlist_var = tk.StringVar(value="(default)")
        self.bulk_playlist_combo = ttk.Combobox(
            row, textvariable=self.bulk_playlist_var, width=20, state="readonly",
            values=["(default)", "(none)"])
        self.bulk_playlist_combo.pack(side="left")
        ttk.Button(row, text="Set",
                   command=self.bulk_apply_playlist).pack(side="left", padx=(4, 0))

        row2 = ttk.Frame(bulk)
        row2.pack(fill="x", padx=6, pady=(2, 6))
        ttk.Button(row2, text="✅ Verify on YouTube",
                   command=self.bulk_verify).pack(side="left")
        ttk.Button(row2, text="↺ Reset upload state",
                   command=self.bulk_reset_state).pack(side="left", padx=(6, 0))
        vsep(row2)
        ttk.Button(row2, text="🗑 Recycle local files",
                   command=self.bulk_recycle).pack(side="left")
        ttk.Button(row2, text="🗑 Delete local files (any status)",
                   command=self.bulk_delete_local).pack(side="left", padx=(6, 0))
        if self.runtime.setting("lsdvr_delete"):
            ttk.Button(row2, text="⚠ Force local delete (skip LiveStreamDVR)",
                       command=self.bulk_delete_local_force).pack(side="left", padx=(6, 0))
        self.op_status_label = ttk.Label(row2, text="", style="Muted.TLabel")
        self.op_status_label.pack(side="left", padx=10)

        editor = ttk.LabelFrame(tab, text="📝 Video metadata (edit before queueing)")
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

        row2b = ttk.Frame(editor)
        row2b.pack(fill="x", padx=6, pady=2)
        ttk.Label(row2b, text="Tags (comma separated):").pack(side="left")
        self.tags_var = tk.StringVar()
        ttk.Entry(row2b, textvariable=self.tags_var).pack(
            side="left", fill="x", expand=True, padx=6)
        ttk.Label(row2b, text="Privacy:").pack(side="left")
        self.privacy_var = tk.StringVar(value=self.runtime.setting("privacy"))
        ttk.Combobox(row2b, textvariable=self.privacy_var, width=9, state="readonly",
                     values=("private", "unlisted", "public")).pack(side="left", padx=(4, 0))
        ttk.Label(row2b, text="Playlist:").pack(side="left", padx=(10, 0))
        self.playlist_choice_var = tk.StringVar(value="(default)")
        self.editor_playlist_combo = ttk.Combobox(
            row2b, textvariable=self.playlist_choice_var, width=22, state="readonly",
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
        ttk.Button(btns, text="♻ Reset to generated metadata",
                   command=self._regenerate_selected).pack(side="left")
        ttk.Button(btns, text="▶ Play video",
                   command=self.play_selected_video).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="📂 Open folder",
                   command=self.open_selected_folder).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="➕ Add selected to queue", style="Accent.TButton",
                   command=self.add_selected_to_queue).pack(side="right")

    def pick_folder(self) -> None:
        folder = filedialog.askdirectory(
            initialdir=self.folder_var.get() or None,
            title="Pick the folder that contains the VOD subfolders")
        if folder:
            self.folder_var.set(folder)
            self.scan_folder()

    def scan_folder(self) -> None:
        folder = self.folder_var.get().strip()
        if not folder:
            return
        if not Path(folder).is_dir():
            messagebox.showerror("Scan", f"Folder does not exist:\n{folder}")
            return
        self._save_editor()
        self._editing_key = None
        try:
            self.runtime.scan_async(folder)
        except RuntimeError as exc:
            messagebox.showinfo("Scan", str(exc))
            return
        self.scan_btn.configure(state="disabled")
        self.scan_status_label.configure(text="⏳ Scanning…")

    def _refresh_scan_label(self) -> None:
        scan = self.runtime.scan_state
        self.scan_btn.configure(state="disabled" if scan.get("active") else "normal")
        if scan.get("active"):
            total = scan.get("total") or 0
            self.scan_status_label.configure(
                text=f"⏳ Scanning… {scan.get('done', 0)}/{total}: "
                     f"{str(scan.get('current', ''))[:48]}" if total else "⏳ Scanning…")
        elif scan.get("error"):
            self.scan_status_label.configure(text=f"❌ Scan failed: {scan['error']}")
        elif scan.get("found") == 0:
            self.scan_status_label.configure(
                text="⚠ No VOD folders found here — pick the folder that "
                     "contains the per-stream subfolders.")
        elif scan.get("found") is not None:
            self.scan_status_label.configure(
                text=f"✅ Found {scan['found']} VOD folder(s).")

    def _refresh_op_label(self) -> None:
        op = self.runtime.op_state
        active = bool(op.get("active"))
        if active:
            self.op_status_label.configure(
                text=f"🗑 {op.get('label', 'Working')}… {op.get('done', 0)}/{op.get('total', 0)}")
        elif op.get("result"):
            total = op.get("total", 0)
            ok = op.get("ok_count", total)
            failed = bool(total) and ok == 0
            self.op_status_label.configure(
                text=("❌ " if failed else "✅ ") + op["result"])
            if failed and self._op_was_active:
                messagebox.showwarning(
                    f"{op.get('label', 'Operation')} failed",
                    f"{op['result']} — nothing was removed. See the Log "
                    "below for the reason(s).")
        self._op_was_active = active

    def _generate_meta(self, vod: Vod) -> dict:
        return self.runtime._generate_meta(vod)

    def _video_status_category(self, vod: Vod) -> str:
        entry = self.app.manager.registry.get(vod.key)
        if entry:
            if entry.get("failed"):
                return "failed"
            return "verified" if entry.get("verified") else "uploaded"
        for item in self.runtime.queue_items:
            if item.key == vod.key and item.status in ("queued", "uploading", "verifying"):
                return "queued"
        if vod.problems:
            return "not_finalized"
        return "ready"

    def _video_status(self, vod: Vod) -> tuple[str, str]:
        entry = self.app.manager.registry.get(vod.key)
        category = self._video_status_category(vod)
        if category == "failed":
            return "failed on YouTube — requeue or reset", "problem"
        if category in ("uploaded", "verified"):
            if entry.get("local_deleted"):
                return "uploaded ✓ · recycled", "uploaded"
            if category == "verified":
                return "uploaded ✓ verified", "uploaded"
            return "uploaded (unverified)", "uploaded"
        if category == "queued":
            for item in self.runtime.queue_items:
                if item.key == vod.key and item.status in (
                        "queued", "uploading", "verifying"):
                    return item.status, ""
        if category == "not_finalized":
            return ", ".join(vod.problems), "problem"
        return "ready", ""

    @staticmethod
    def _parse_filter_date(text: str):
        text = text.strip()
        if not text:
            return None
        try:
            return datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            return None

    def _filtered_vods(self) -> list[Vod]:
        text = self.video_filter_text.get().strip().lower()
        status = self.video_filter_status.get()
        game = self.video_filter_game.get()
        date_from = self._parse_filter_date(self.video_filter_date_from.get())
        date_to = self._parse_filter_date(self.video_filter_date_to.get())
        result = []
        for vod in self.runtime.vods.values():
            if text:
                haystack = f"{vod.streamer_name} {vod.streamer_login} {vod.stream_title}"
                if text not in haystack.lower():
                    continue
            if status != "all" and self._video_status_category(vod) != status:
                continue
            if game != "(all)" and game not in vod.games:
                continue
            if date_from or date_to:
                if vod.started_at is None:
                    continue
                d = vod.started_at.date()
                if date_from and d < date_from:
                    continue
                if date_to and d > date_to:
                    continue
            result.append(vod)
        return result

    def _on_video_filter_changed(self, *_args) -> None:
        self._refresh_video_tree()

    def _on_video_search_typed(self) -> None:
        if self._video_search_after_id is not None:
            self.app.root.after_cancel(self._video_search_after_id)
        self._video_search_after_id = self.app.root.after(
            250, self._on_video_filter_changed)

    def _clear_video_filters(self) -> None:
        self.video_filter_text.set("")
        self.video_filter_status.set("all")
        self.video_filter_game.set("(all)")
        self.video_filter_date_from.set("")
        self.video_filter_date_to.set("")
        self._refresh_video_tree()

    def _open_calendar_picker(self, var: tk.StringVar, anchor: tk.Widget) -> None:
        import calendar as cal_mod
        try:
            base = datetime.strptime(var.get().strip(), "%Y-%m-%d").date()
        except ValueError:
            base = datetime.now().date()
        state = {"year": base.year, "month": base.month}
        c = self.app.colors

        dlg = tk.Toplevel(self.app.root)
        dlg.title("Pick a date")
        dlg.configure(bg=c["bg"])
        dlg.transient(self.app.root)
        dlg.resizable(False, False)
        dlg.geometry(f"+{anchor.winfo_rootx()}"
                     f"+{anchor.winfo_rooty() + anchor.winfo_height() + 4}")

        header = ttk.Frame(dlg)
        header.pack(fill="x", padx=8, pady=(8, 2))
        title_lbl = ttk.Label(header, anchor="center")
        ttk.Button(header, text="‹", width=3, command=lambda: shift(-1)
                   ).pack(side="left")
        title_lbl.pack(side="left", expand=True, fill="x")
        ttk.Button(header, text="›", width=3, command=lambda: shift(1)
                   ).pack(side="left")

        grid = ttk.Frame(dlg)
        grid.pack(padx=8, pady=4)

        def commit(value: str) -> None:
            var.set(value)
            dlg.destroy()
            self._on_video_filter_changed()

        def render() -> None:
            for w in grid.winfo_children():
                w.destroy()
            title_lbl.configure(
                text=f"{cal_mod.month_name[state['month']]} {state['year']}")
            for col, wd in enumerate(("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")):
                ttk.Label(grid, text=wd, style="Muted.TLabel", width=3,
                         anchor="center").grid(row=0, column=col)
            today = datetime.now().date()
            weeks = cal_mod.Calendar(firstweekday=0).monthdayscalendar(
                state["year"], state["month"])
            for row_i, week in enumerate(weeks, start=1):
                for col, day in enumerate(week):
                    if day == 0:
                        continue
                    is_today = (day, state["month"], state["year"]) == \
                        (today.day, today.month, today.year)
                    ttk.Button(
                        grid, text=str(day), width=3,
                        style="Accent.TButton" if is_today else "TButton",
                        command=lambda d=day: commit(
                            f"{state['year']:04d}-{state['month']:02d}-{d:02d}")
                    ).grid(row=row_i, column=col, padx=1, pady=1)

        def shift(delta: int) -> None:
            m = state["month"] + delta
            y = state["year"]
            if m < 1:
                m, y = 12, y - 1
            elif m > 12:
                m, y = 1, y + 1
            state["month"], state["year"] = m, y
            render()

        footer = ttk.Frame(dlg)
        footer.pack(fill="x", padx=8, pady=(2, 8))
        ttk.Button(footer, text="Today",
                   command=lambda: commit(datetime.now().strftime("%Y-%m-%d"))
                   ).pack(side="left")
        ttk.Button(footer, text="Clear", command=lambda: commit("")
                   ).pack(side="left", padx=(4, 0))
        ttk.Button(footer, text="Close", command=dlg.destroy).pack(side="right")

        render()
        dlg.grab_set()

    def _update_video_summary(self, filtered: list[Vod]) -> None:
        counts = {"ready": 0, "queued": 0, "uploaded": 0, "verified": 0,
                 "failed": 0, "not_finalized": 0}
        for vod in self.runtime.vods.values():
            cat = self._video_status_category(vod)
            counts[cat] = counts.get(cat, 0) + 1
        shown_size = sum(vod.size_bytes for vod in filtered if vod.size_bytes)
        text = (f"{len(filtered)} shown of {len(self.runtime.vods)} total "
                f"({fmt_size(shown_size)}) — "
                f"{counts['ready']} ready, {counts['queued']} queued, "
                f"{counts['uploaded']} uploaded, {counts['verified']} verified, "
                f"{counts['failed']} failed, {counts['not_finalized']} not finalized")
        if self.video_checked:
            checked_size = sum(self.runtime.vods[k].size_bytes for k in self.video_checked
                               if k in self.runtime.vods and self.runtime.vods[k].size_bytes)
            text += (f" · {len(self.video_checked)} checked "
                    f"({fmt_size(checked_size)})")
        self.video_summary_label.configure(text=text)

    def _refresh_video_tree(self) -> None:
        selected = set(self.video_tree.selection())
        self.video_tree.delete(*self.video_tree.get_children())
        filtered = self._filtered_vods()
        for i, vod in enumerate(filtered):
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
        self._update_video_summary(filtered)

    def _set_all_videos_checked(self, checked: bool) -> None:
        visible = {vod.key for vod in self._filtered_vods()}
        if checked:
            self.video_checked |= visible
        else:
            self.video_checked -= visible
        for key in self.video_tree.get_children():
            self.video_tree.set(key, "check",
                                CHECKED if key in self.video_checked else UNCHECKED)
        self._update_video_summary(self._filtered_vods())

    def _toggle_all_videos(self) -> None:
        visible = {vod.key for vod in self._filtered_vods()}
        self._set_all_videos_checked(not (visible and visible <= self.video_checked))

    def _checked_video_keys(self) -> list[str]:
        return [k for k in self.runtime.vods if k in self.video_checked]

    # ------------------------------------------------------------ bulk ops --
    def bulk_add_checked(self) -> None:
        self._save_editor()
        keys = self._checked_video_keys()
        if not keys:
            messagebox.showinfo("Bulk", "No rows checked — click the ☐ column first.")
            return
        self._do_enqueue(keys)

    def _do_enqueue(self, keys: list[str]) -> None:
        added, skipped = self.runtime.enqueue(keys)
        self._refresh_queue_tree()
        self._refresh_video_tree()
        if added:
            host = self._nb if self._nb is not None else self.app.notebook
            host.select(self.queue_tab_frame)

    def bulk_reset_meta(self) -> None:
        keys = self._checked_video_keys()
        for key in keys:
            self.runtime.metas[key] = self._generate_meta(self.runtime.vods[key])
        if self._editing_key in keys:
            self._editing_key = None
            self._on_video_select()
        self.runtime._save_queue_state()
        self._refresh_video_tree()

    def bulk_apply_privacy(self) -> None:
        privacy = self.bulk_privacy_var.get()
        keys = self._checked_video_keys()
        for key in keys:
            self.runtime.metas[key]["privacy"] = privacy
        if self._editing_key in keys:
            self.privacy_var.set(privacy)
        self.runtime._save_queue_state()

    def bulk_apply_playlist(self) -> None:
        choice = self.bulk_playlist_var.get() or "(default)"
        keys = self._checked_video_keys()
        for key in keys:
            self.runtime.metas[key]["playlist_choice"] = choice
        if self._editing_key in keys:
            self.playlist_choice_var.set(choice)
        self.runtime._save_queue_state()

    def bulk_verify(self) -> None:
        keys = self._checked_video_keys()
        try:
            self.runtime.bulk("verify", keys)
        except RuntimeError as exc:
            messagebox.showinfo("Verify", str(exc))

    def bulk_recycle(self) -> None:
        keys = self._checked_video_keys()
        candidates = [k for k in keys
                     if (e := self.app.manager.registry.get(k))
                     and e.get("verified") and not e.get("local_deleted")]
        if not candidates:
            messagebox.showinfo(
                "Recycle", "Nothing to recycle. Only checked videos that were "
                "uploaded AND verified on YouTube can be recycled.\n"
                "Use “Verify on YouTube” first if needed.")
            return
        fallback_note = ""
        if self.runtime.setting("lsdvr_delete"):
            fallback_note = (
                "\n\nLiveStreamDVR deletion is enabled for this queue — these "
                "VODs will be deleted permanently through the LiveStreamDVR "
                "API instead of the Recycle Bin.")
        elif self.runtime.setting("recycle_fallback_delete"):
            fallback_note = (
                "\n\n⚠ Permanent-delete fallback is enabled — if the Recycle "
                "Bin isn't available for a file, it will be DELETED "
                "PERMANENTLY instead.")
        if not messagebox.askyesno(
                "Recycle", f"Move {len(candidates)} whole VOD folder(s) — video, "
                "chapters, metadata, everything — to the Recycle Bin?"
                + fallback_note):
            return
        try:
            self.runtime.bulk("delete_local", candidates)
        except RuntimeError as exc:
            messagebox.showinfo("Recycle", str(exc))

    def bulk_delete_local(self) -> None:
        keys = self._checked_video_keys()
        candidates = [k for k in keys
                     if not (self.app.manager.registry.get(k) or {}).get("local_deleted")]
        if not candidates:
            messagebox.showinfo(
                "Delete local files",
                "No checked rows have a local folder to delete "
                "(or it was already removed).")
            return
        unverified = sum(
            1 for k in candidates
            if not (self.app.manager.registry.get(k) or {}).get("verified"))
        warn = ""
        if unverified:
            warn = (f"\n\n⚠ {unverified} of these {len(candidates)} video(s) "
                    "have NOT been confirmed uploaded and verified on "
                    "YouTube — deleting them now risks losing the only copy.")
        if self.runtime.setting("lsdvr_delete"):
            warn += ("\n\nLiveStreamDVR deletion is enabled for this queue — "
                     "these VODs will be deleted permanently through the "
                     "LiveStreamDVR API instead of the Recycle Bin.")
        elif self.runtime.setting("recycle_fallback_delete"):
            warn += ("\n\n⚠ Permanent-delete fallback is enabled — if the "
                     "Recycle Bin isn't available for a file, it will be "
                     "DELETED PERMANENTLY instead.")
        if not messagebox.askyesno(
                "Delete local files",
                f"Delete {len(candidates)} whole VOD folder(s) — video, "
                "chapters, metadata, everything?" + warn, icon="warning"):
            return
        try:
            self.runtime.bulk("delete_local_any", candidates)
        except RuntimeError as exc:
            messagebox.showinfo("Delete local files", str(exc))

    def bulk_delete_local_force(self) -> None:
        """Rescue action for when LiveStreamDVR itself can't do the deletion
        (unreachable, its own server out of disk space, ...): skips the API
        and deletes the local files directly, same as with LiveStreamDVR
        deletion turned off."""
        keys = self._checked_video_keys()
        candidates = [k for k in keys
                     if not (self.app.manager.registry.get(k) or {}).get("local_deleted")]
        if not candidates:
            messagebox.showinfo(
                "Force local delete",
                "No checked rows have a local folder to delete "
                "(or it was already removed).")
            return
        if not messagebox.askyesno(
                "Force local delete — bypass LiveStreamDVR",
                f"Delete {len(candidates)} whole VOD folder(s) directly from disk, "
                "skipping LiveStreamDVR entirely?\n\n"
                "⚠ Only do this if LiveStreamDVR itself can't delete them (its "
                "server is unreachable or out of disk space, etc.) — if "
                "LiveStreamDVR is still actively watching/recording any of "
                "these folders, deleting them behind its back can crash it.",
                icon="warning"):
            return
        try:
            self.runtime.bulk("delete_local_force", candidates)
        except RuntimeError as exc:
            messagebox.showinfo("Force local delete", str(exc))

    def bulk_reset_state(self) -> None:
        keys = self._checked_video_keys()
        try:
            result = self.runtime.bulk("reset_state", keys)
        except RuntimeError as exc:
            messagebox.showinfo("Reset", str(exc))
            return
        if not result["count"]:
            messagebox.showinfo(
                "Reset", "None of the checked rows have an upload record to reset.")
            return
        if not messagebox.askyesno(
                "Reset upload state",
                f"Forget the upload record of {result['count']} video(s)?\n\n"
                "They will show as 'ready' again and can be re-uploaded. "
                "Videos already on YouTube are NOT touched — this only clears "
                "the app's own bookkeeping, so re-uploading may create duplicates."):
            return
        self._refresh_video_tree()

    # ----------------------------------------------------------- selection --
    def _selected_vod(self):
        sel = self.video_tree.selection()
        return self.runtime.vods.get(sel[0]) if sel else None

    def _open_vod_folder(self, event) -> None:
        if self.video_tree.identify("region", event.x, event.y) != "cell":
            return
        from .gui_common import open_in_file_manager
        sel = self.video_tree.selection()
        if sel and sel[0] in self.runtime.vods:
            open_in_file_manager(self.runtime.vods[sel[0]].folder)

    def play_selected_video(self) -> None:
        from .gui_common import open_in_file_manager
        vod = self._selected_vod()
        if vod is None:
            messagebox.showinfo("Play", "Select a video in the list first.")
            return
        if vod.video_path is None or not vod.video_path.exists():
            messagebox.showinfo("Play", "This VOD has no video file.")
            return
        open_in_file_manager(vod.video_path)

    def open_selected_folder(self) -> None:
        from .gui_common import open_in_file_manager
        vod = self._selected_vod()
        if vod is None:
            messagebox.showinfo("Open folder", "Select a video in the list first.")
            return
        open_in_file_manager(vod.folder)

    def _on_video_select(self, _event=None) -> None:
        self._save_editor()
        sel = self.video_tree.selection()
        if len(sel) != 1 or sel[0] not in self.runtime.vods:
            self._editing_key = None
            return
        key = sel[0]
        meta = self.runtime.metas[key]
        self._editing_key = None
        self.title_var.set(meta["title"])
        self.tags_var.set(meta["tags"])
        self.privacy_var.set(meta["privacy"])
        self.playlist_choice_var.set(meta.get("playlist_choice", "(default)"))
        self.desc_text.delete("1.0", "end")
        self.desc_text.insert("1.0", meta["description"])
        self._editing_key = key

    def _save_editor(self) -> None:
        if not self._editing_key or self._editing_key not in self.runtime.metas:
            return
        meta = self.runtime.metas[self._editing_key]
        meta["title"] = scanner.sanitize_title(self.title_var.get())
        meta["tags"] = self.tags_var.get().strip()
        meta["privacy"] = self.privacy_var.get()
        meta["playlist_choice"] = self.playlist_choice_var.get() or "(default)"
        meta["description"] = self.desc_text.get("1.0", "end-1c")
        self.runtime._save_queue_state()

    def _update_title_count(self) -> None:
        n = len(self.title_var.get())
        self.title_count.configure(
            text=f"{n}/100", foreground=self.app.colors["err"] if n > 100 else "")

    def _regenerate_selected(self) -> None:
        sel = self.video_tree.selection()
        for key in sel:
            if key in self.runtime.vods:
                self.runtime.metas[key] = self._generate_meta(self.runtime.vods[key])
        if len(sel) == 1:
            self._editing_key = None
            self._on_video_select()
        self.runtime._save_queue_state()

    def add_selected_to_queue(self) -> None:
        self._save_editor()
        keys = [k for k in self.video_tree.selection() if k in self.runtime.vods]
        self._do_enqueue(keys)

    # -------------------------------------------------------------- queue --
    def _build_queue_tab(self, host) -> None:
        tab = ttk.Frame(host)
        self._add_tab(host, tab, " 📤 Queue & Progress ")
        self.queue_tab_frame = tab

        header = ttk.Frame(tab)
        header.pack(fill="x", padx=8, pady=(8, 0))
        self.queue_header_label = ttk.Label(header, text="", style="Muted.TLabel")
        self.queue_header_label.pack(anchor="w")

        cols = ("check", "pos", "title", "size", "privacy", "status", "detail")
        self.queue_tree = ttk.Treeview(tab, columns=cols, show="headings",
                                       selectmode="browse", height=12)
        headings = {"check": (UNCHECKED, 46), "pos": ("#", 40),
                    "title": ("Title", 330), "size": ("Size", 85),
                    "privacy": ("Privacy", 70), "status": ("Status", 90),
                    "detail": ("Progress / result", 280)}
        for col, (text, width) in headings.items():
            self.queue_tree.heading(col, text=text)
            self.queue_tree.column(col, width=width,
                                   anchor="center" if col in ("check", "pos", "size", "privacy", "status") else "w")
        self.queue_tree.heading("check", command=self._toggle_all_queue)
        self.queue_checked: set[str] = set()
        self.app._bind_checkbox_column(self.queue_tree, self.queue_checked)
        self.queue_tree.bind("<Double-1>", self._on_queue_double)
        check_all_queue = self.app._check_all_shortcut(self._set_all_queue_checked)
        self.queue_tree.bind("<Control-a>", check_all_queue)
        self.queue_tree.bind("<Command-a>", check_all_queue)

        def remove_via_delete_key(_event=None):
            self.remove_queue_item()
            return "break"

        self.queue_tree.bind("<Delete>", remove_via_delete_key)
        self.queue_tree.bind("<BackSpace>", remove_via_delete_key)
        self.queue_tree.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        ctl = ttk.Frame(tab)
        ctl.pack(fill="x", padx=8, pady=4)
        self.start_btn = ttk.Button(ctl, text="▶ Start uploads", style="Accent.TButton",
                                    command=self.start_uploads)
        self.start_btn.pack(side="left")
        self.pause_btn = ttk.Button(ctl, text="⏸ Pause",
                                    command=self.pause_uploads, state="disabled")
        self.pause_btn.pack(side="left", padx=(6, 0))
        self.cancel_btn = ttk.Button(ctl, text="✖ Cancel current",
                                     command=self.cancel_current, state="disabled")
        self.cancel_btn.pack(side="left", padx=(6, 0))
        ttk.Separator(ctl, orient="vertical").pack(side="left", fill="y",
                                                   padx=10, pady=2)
        ttk.Button(ctl, text="↻ Retry failed", command=self.retry_failed
                   ).pack(side="left")
        ttk.Button(ctl, text="🌐 Open on YouTube", command=self.open_queue_video
                   ).pack(side="left", padx=(6, 0))
        ttk.Button(ctl, text="🗑 Remove", command=self.remove_queue_item
                   ).pack(side="right")
        ttk.Button(ctl, text="🧹 Clear finished", command=self.clear_finished
                   ).pack(side="right", padx=(0, 6))
        ttk.Button(ctl, text="▼", width=3, command=lambda: self.move_queue_item(1)
                   ).pack(side="right", padx=(0, 10))
        ttk.Button(ctl, text="▲", width=3, command=lambda: self.move_queue_item(-1)
                   ).pack(side="right", padx=(0, 4))

        self.quota_label = ttk.Label(tab, text="", style="Muted.TLabel")
        self.quota_label.pack(anchor="w", padx=8, pady=(0, 4))
        self.cooldown_label = ttk.Label(tab, text="", style="Muted.TLabel")
        self.cooldown_label.pack(anchor="w", padx=8, pady=(0, 4))

        prog = ttk.Frame(tab)
        prog.pack(fill="x", padx=8, pady=4)
        self.current_label = ttk.Label(prog, text="Idle.")
        self.current_label.pack(anchor="w")
        self.progressbar = ttk.Progressbar(prog, maximum=100.0)
        self.progressbar.pack(fill="x", pady=(2, 0))

    def _refresh_queue_tree(self) -> None:
        self.queue_checked &= {i.key for i in self.runtime.queue_items}
        self.queue_tree.delete(*self.queue_tree.get_children())
        for idx, item in enumerate(self.runtime.queue_items, start=1):
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
        pending = sum(1 for i in self.runtime.queue_items if i.status == "queued")
        done = sum(1 for i in self.runtime.queue_items if i.status == "done")
        self.queue_header_label.configure(
            text=f"Queue: {pending} pending, {done} done, "
                 f"{len(self.runtime.queue_items)} total")
        self._update_quota_panel(pending)
        self._refresh_cooldown_label()
        self._update_start_buttons()

    def _update_quota_panel(self, pending: int) -> None:
        registry = self.app.manager.registry
        used = limits.units_used_today(registry)
        remaining = limits.uploads_remaining_today(registry)
        text = f"YouTube quota today (shared by every queue): {used:,} / {limits.QUOTA_DAILY_UNITS:,} units"
        if pending:
            eta = limits.eta_for_queue(pending, remaining)
            text += (f" · ~{remaining} upload(s) left today · "
                    f"queue of {pending} finishes {eta} (resets at Pacific midnight)")
        self.quota_label.configure(text=text)

    def _refresh_cooldown_label(self) -> None:
        own = limits.get_cooldown(self.runtime._state)
        quota = limits.get_quota_cooldown(self.app.manager.cfg)
        parts = []
        if quota is not None:
            parts.append(f"⏳ Shared API-quota cooldown until {limits.fmt_local(quota)} "
                        "— every queue waits for this one.")
        if own is not None:
            parts.append(f"⏳ This queue's channel limit — cooldown until "
                        f"{limits.fmt_local(own)} ({self.runtime._state.get('cooldown_reason', '')})"
                        " — other queues are unaffected.")
        self.cooldown_label.configure(text=" ".join(parts))

    def _update_start_buttons(self) -> None:
        alive = bool(self.runtime.worker and self.runtime.worker.is_alive())
        self.start_btn.configure(state="disabled" if alive else "normal")
        self.pause_btn.configure(state="normal" if alive else "disabled")
        self.cancel_btn.configure(state="normal" if alive else "disabled")

    def _set_all_queue_checked(self, checked: bool) -> None:
        self.queue_checked.clear()
        if checked:
            self.queue_checked.update(i.key for i in self.runtime.queue_items)
        for key in self.queue_tree.get_children():
            self.queue_tree.set(
                key, "check", CHECKED if key in self.queue_checked else UNCHECKED)

    def _toggle_all_queue(self) -> None:
        all_keys = {i.key for i in self.runtime.queue_items}
        self._set_all_queue_checked(not (self.queue_checked >= all_keys))

    def _selected_queue_index(self) -> int | None:
        sel = self.queue_tree.selection()
        if not sel:
            return None
        for i, item in enumerate(self.runtime.queue_items):
            if item.key == sel[0]:
                return i
        return None

    def remove_queue_item(self) -> None:
        keys = set(self.queue_checked)
        if not keys:
            sel = self.queue_tree.selection()
            if not sel:
                return
            keys = {sel[0]}
        busy = [i for i in self.runtime.queue_items
                if i.key in keys and i.status in ("uploading", "verifying")]
        if busy:
            messagebox.showinfo("Queue", "A checked video is currently uploading — "
                                "use “Cancel current upload” first.")
            keys -= {i.key for i in busy}
        self.runtime.remove_from_queue(list(keys))
        self._refresh_queue_tree()
        self._refresh_video_tree()

    def move_queue_item(self, delta: int) -> None:
        idx = self._selected_queue_index()
        if idx is None:
            return
        key = self.runtime.queue_items[idx].key
        self.runtime.move_in_queue(key, delta)
        self._refresh_queue_tree()
        if self.queue_tree.exists(key):
            self.queue_tree.selection_set(key)

    def clear_finished(self) -> None:
        self.runtime.clear_finished()
        self._refresh_queue_tree()

    def start_uploads(self) -> None:
        if self.runtime.worker and self.runtime.worker.is_alive():
            return
        if not any(i.status == "queued" for i in self.runtime.queue_items):
            messagebox.showinfo("Upload", "Queue is empty — add videos on the Videos tab.")
            return
        own_cd = limits.get_cooldown(self.runtime._state)
        quota_cd = limits.get_quota_cooldown(self.app.manager.cfg)
        if own_cd is not None or quota_cd is not None:
            reason = (f"the shared API-quota cooldown until {limits.fmt_local(quota_cd)}"
                     if quota_cd is not None else
                     f"this queue's channel limit until {limits.fmt_local(own_cd)} "
                     f"({self.runtime._state.get('cooldown_reason', '')})")
            if not messagebox.askyesno(
                    "Upload cooldown",
                    f"Upload cooldown is active — {reason}.\nUploads resume "
                    "automatically then.\n\nStart anyway now?"):
                return
            force = True
        else:
            force = False
        try:
            self.app.manager.request_start(self.runtime.id, force=force)
        except RuntimeError as exc:
            messagebox.showwarning("Upload", str(exc))
            return
        self._update_start_buttons()

    def pause_uploads(self) -> None:
        self.runtime.pause_uploads()
        self.pause_btn.configure(state="disabled")

    def cancel_current(self) -> None:
        self.runtime.cancel_current()

    def open_queue_video(self) -> None:
        sel = self.queue_tree.selection()
        item = self._item_by_key(sel[0]) if sel else None
        if item is None:
            messagebox.showinfo("Open", "Select a video in the queue first.")
            return
        if not item.video_id:
            messagebox.showinfo("Open", "That video hasn't been uploaded yet — "
                                "the link appears once the upload finishes.")
            return
        import webbrowser
        webbrowser.open(f"https://youtu.be/{item.video_id}")

    def _on_queue_double(self, event) -> None:
        if self.queue_tree.identify("region", event.x, event.y) != "cell":
            return
        key = self.queue_tree.identify_row(event.y)
        item = self._item_by_key(key) if key else None
        if item and item.video_id:
            import webbrowser
            webbrowser.open(f"https://youtu.be/{item.video_id}")

    def retry_failed(self) -> None:
        self.runtime.retry_failed()
        self._refresh_queue_tree()
        self._refresh_video_tree()

    def _item_by_key(self, key: str):
        for item in self.runtime.queue_items:
            if item.key == key:
                return item
        return None

    def _update_queue_row(self, item) -> None:
        if self.queue_tree.exists(item.key):
            detail = f"{item.progress:.1f}%  {item.detail}"
            self.queue_tree.set(item.key, "detail", detail)
            self.queue_tree.set(item.key, "status", item.status)

    # --------------------------------------------------------- automation --
    def _build_automation_tab(self, host) -> None:
        tab = ttk.Frame(host)
        self._add_tab(host, tab, " 🤖 Automation ")
        self.automation_tab_frame = tab

        box = ttk.LabelFrame(tab, text="📁 Background folder watching")
        box.pack(fill="x", padx=10, pady=10)

        self.auto_enabled_var = tk.BooleanVar(value=bool(self.runtime.setting("auto_scan")))
        ttk.Checkbutton(box, text="Watch the VOD folder and rescan automatically",
                        variable=self.auto_enabled_var,
                        command=self._automation_changed).pack(anchor="w", padx=8, pady=(8, 2))

        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=2)
        ttk.Label(row, text="Scan every").pack(side="left")
        self.auto_interval_var = tk.StringVar(
            value=str(self.runtime.setting("auto_scan_interval_min", 10)))
        ttk.Spinbox(row, from_=1, to=1440, textvariable=self.auto_interval_var,
                    width=5, command=self._automation_changed).pack(side="left", padx=4)
        ttk.Label(row, text="minutes").pack(side="left")

        self.auto_queue_var = tk.BooleanVar(value=bool(self.runtime.setting("auto_queue", True)))
        ttk.Checkbutton(box, text="Automatically queue new ready VODs "
                                  "(with generated metadata and the default playlist rule)",
                        variable=self.auto_queue_var, command=self._automation_changed
                        ).pack(anchor="w", padx=8, pady=2)
        self.auto_start_var = tk.BooleanVar(value=bool(self.runtime.setting("auto_start", True)))
        ttk.Checkbutton(box, text="Automatically start uploading when the queue has items",
                        variable=self.auto_start_var, command=self._automation_changed
                        ).pack(anchor="w", padx=8, pady=2)
        self.auto_finalized_var = tk.BooleanVar(
            value=bool(self.runtime.setting("auto_only_finalized", True)))
        ttk.Checkbutton(box, text="Skip raw .ts / not-finalized captures (recommended — "
                                  "they may still be recording)",
                        variable=self.auto_finalized_var, command=self._automation_changed
                        ).pack(anchor="w", padx=8, pady=(2, 8))

        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=(0, 8))
        self.auto_status_label = ttk.Label(row, text="Automation is off.")
        self.auto_status_label.pack(side="left")
        ttk.Button(row, text="Run a scan cycle now", command=self._run_auto_cycle_now
                   ).pack(side="right")

    def _refresh_automation_fields(self) -> None:
        self.auto_enabled_var.set(bool(self.runtime.setting("auto_scan")))
        self.auto_interval_var.set(str(self.runtime.setting("auto_scan_interval_min", 10)))
        self.auto_queue_var.set(bool(self.runtime.setting("auto_queue", True)))
        self.auto_start_var.set(bool(self.runtime.setting("auto_start", True)))
        self.auto_finalized_var.set(bool(self.runtime.setting("auto_only_finalized", True)))
        self._refresh_automation_status()

    def _refresh_automation_status(self) -> None:
        if self.runtime.setting("auto_scan"):
            self.auto_status_label.configure(
                text=f"Automation is ON — {self.runtime.auto_status}")
        else:
            self.auto_status_label.configure(text="Automation is off.")

    def _automation_changed(self) -> None:
        patch = {"auto_scan": bool(self.auto_enabled_var.get()),
                 "auto_queue": bool(self.auto_queue_var.get()),
                 "auto_start": bool(self.auto_start_var.get()),
                 "auto_only_finalized": bool(self.auto_finalized_var.get())}
        try:
            patch["auto_scan_interval_min"] = max(1, int(self.auto_interval_var.get()))
        except ValueError:
            pass
        self.app.manager.update_queue(self.runtime.id, patch)
        self.runtime._auto_countdown = patch.get(
            "auto_scan_interval_min",
            self.runtime.setting("auto_scan_interval_min", 10)) * 60
        self._refresh_automation_status()

    def _run_auto_cycle_now(self) -> None:
        self.app.manager.auto_cycle(self.runtime.id)

    # ----------------------------------------------------------------- events --
    def handle_event(self, ev: dict) -> None:
        etype = ev.get("type")
        if etype == "scan_progress":
            self._refresh_scan_label()
        elif etype == "scan_done":
            self._refresh_scan_label()
            games = sorted({g for vod in self.runtime.vods.values() for g in vod.games})
            self.video_filter_game_combo.configure(values=["(all)"] + games)
            if self.video_filter_game.get() not in ("(all)", *games):
                self.video_filter_game.set("(all)")
            self._refresh_video_tree()
            self._refresh_queue_tree()
        elif etype == "item_status":
            item = self._item_by_key(ev["key"])
            if item and ev["status"] == "uploading":
                self.current_label.configure(text=f"Uploading: {item.title}")
                self._reset_progress_anim()
            if item and ev["status"] == "verifying":
                self.current_label.configure(text=f"Verifying on YouTube: {item.title}")
            if item and ev["status"] == "done":
                self._reset_progress_anim()
                self.current_label.configure(text="Idle.")
            self._refresh_queue_tree()
            self._refresh_video_tree()
        elif etype == "item_detail":
            item = self._item_by_key(ev["key"])
            if item:
                self._update_queue_row(item)
        elif etype == "progress":
            item = self._item_by_key(ev["key"])
            if item:
                size = item.vod.size_bytes or 1
                self._prog_anim.update(
                    active=True, pct=ev["pct"], ts=__import__("time").monotonic(),
                    pct_per_s=(ev["speed_bps"] / size * 100.0) if ev["speed_bps"] else 0.0)
                self.current_label.configure(
                    text=f"Uploading: {item.title} — {ev['pct']:.1f}%"
                         + (f" @ {fmt_speed(ev['speed_bps'])}, ETA {fmt_eta(ev['eta_s'])}"
                            if ev["speed_bps"] else ""))
                self._update_queue_row(item)
        elif etype == "worker_done":
            self._update_start_buttons()
            self._reset_progress_anim()
            self._refresh_cooldown_label()
            reason = ev.get("reason")
            self.current_label.configure(text={
                "finished": "Queue finished.", "paused": "Paused.",
                "quota": "⏳ Cooling down — resumes automatically.",
                "daily_limit": "⏳ Daily limit reached — resumes automatically.",
                "error": "⚠ Upload queue stopped — see the log.",
            }.get(reason, "Idle."))
            self._refresh_queue_tree()
        elif etype == "verify_result":
            self._refresh_video_tree()
        elif etype == "queue_started":
            self._update_start_buttons()
        elif etype == "playlists":
            self._update_playlist_choices()

    def _playlist_choices(self) -> list[str]:
        return ["(default)", "(none)"] + [p["title"] for p in self.runtime.playlists]

    def _update_playlist_choices(self) -> None:
        values = self._playlist_choices()
        titles = [p["title"] for p in self.runtime.playlists]
        self.editor_playlist_combo.configure(values=values)
        self.bulk_playlist_combo.configure(values=values)

    def _animate_progress(self) -> None:
        pa = self._prog_anim
        if not pa["active"]:
            return
        import time
        elapsed = time.monotonic() - pa["ts"]
        predicted = min(pa["pct"] + pa["pct_per_s"] * elapsed, 99.7)
        if predicted > pa["shown"]:
            pa["shown"] += (predicted - pa["shown"]) * 0.25
            self.progressbar.configure(value=pa["shown"])

    def _reset_progress_anim(self) -> None:
        self._prog_anim.update(active=False, pct=0.0, pct_per_s=0.0, shown=0.0)
        self.progressbar.configure(value=0)
