"""Tkinter GUI: Videos / Queue & Progress / Settings tabs."""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import auth, config, scanner
from .scanner import Vod
from .uploader import QueueItem, UploadWorker
from .version import __version__

POLL_MS = 100


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
        root.geometry("1050x780")
        root.minsize(860, 620)

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=6, pady=(6, 0))
        self._build_videos_tab()
        self._build_queue_tab()
        self._build_settings_tab()
        self._build_status_bar()

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(POLL_MS, self._poll_events)

        # Try silent sign-in with the saved token
        if config.TOKEN_PATH.exists():
            self._log("Restoring saved YouTube session…")
            threading.Thread(target=self._restore_session, daemon=True).start()
        if self.cfg.get("vod_folder") and Path(self.cfg["vod_folder"]).is_dir():
            self.folder_var.set(self.cfg["vod_folder"])
            self.root.after(200, self.scan_folder)

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

        cols = ("date", "streamer", "title", "duration", "size", "chapters", "status")
        self.video_tree = ttk.Treeview(tab, columns=cols, show="headings",
                                       selectmode="extended", height=10)
        headings = {"date": ("Date", 90), "streamer": ("Streamer", 100),
                    "title": ("Stream title", 330), "duration": ("Length", 70),
                    "size": ("Size", 80), "chapters": ("Chapters", 70),
                    "status": ("Status", 140)}
        for col, (text, width) in headings.items():
            self.video_tree.heading(col, text=text)
            self.video_tree.column(col, width=width,
                                   anchor="center" if col not in ("title",) else "w")
        vsb = ttk.Scrollbar(tab, orient="vertical", command=self.video_tree.yview)
        self.video_tree.configure(yscrollcommand=vsb.set)
        self.video_tree.pack(side="top", fill="both", expand=False, padx=(8, 0))
        vsb.place(in_=self.video_tree, relx=1.0, rely=0, relheight=1.0, anchor="ne")
        self.video_tree.bind("<<TreeviewSelect>>", self._on_video_select)
        self.video_tree.bind("<Double-1>", self._open_vod_folder)
        self.video_tree.tag_configure("uploaded", foreground="#2e7d32")
        self.video_tree.tag_configure("problem", foreground="#b71c1c")

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
        ttk.Button(btns, text="Add selected to queue ▶",
                   command=self.add_selected_to_queue).pack(side="right")
        ttk.Button(btns, text="Add ALL new to queue",
                   command=self.add_all_to_queue).pack(side="right", padx=6)

    # ------------------------------------------------------------- queue tab --
    def _build_queue_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Queue & Progress  ")

        cols = ("pos", "title", "size", "privacy", "status", "detail")
        self.queue_tree = ttk.Treeview(tab, columns=cols, show="headings",
                                       selectmode="browse", height=12)
        headings = {"pos": ("#", 40), "title": ("Title", 360), "size": ("Size", 85),
                    "privacy": ("Privacy", 70), "status": ("Status", 90),
                    "detail": ("Progress / result", 280)}
        for col, (text, width) in headings.items():
            self.queue_tree.heading(col, text=text)
            self.queue_tree.column(col, width=width,
                                   anchor="center" if col in ("pos", "size", "privacy", "status") else "w")
        self.queue_tree.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.queue_tree.tag_configure("done", foreground="#2e7d32")
        self.queue_tree.tag_configure("error", foreground="#b71c1c")
        self.queue_tree.tag_configure("uploading", foreground="#1565c0")

        ctl = ttk.Frame(tab)
        ctl.pack(fill="x", padx=8, pady=4)
        self.start_btn = ttk.Button(ctl, text="▶ Start uploads", command=self.start_uploads)
        self.start_btn.pack(side="left")
        self.pause_btn = ttk.Button(ctl, text="⏸ Pause after current",
                                    command=self.pause_uploads, state="disabled")
        self.pause_btn.pack(side="left", padx=6)
        self.cancel_btn = ttk.Button(ctl, text="✖ Cancel current upload",
                                     command=self.cancel_current, state="disabled")
        self.cancel_btn.pack(side="left")
        ttk.Button(ctl, text="Remove selected", command=self.remove_queue_item
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
                                     command=self.sign_in)
        self.signin_btn.pack(side="left")
        ttk.Button(row, text="Sign out", command=self.sign_out).pack(side="left", padx=6)
        self.account_label = ttk.Label(row, text="Not signed in.")
        self.account_label.pack(side="left", padx=10)

        hint = ("The browser sign-in page is where you pick the Google account AND the "
                "YouTube channel (brand accounts are listed there). To connect a different "
                "channel: Sign out, then Sign in again.")
        ttk.Label(acct, text=hint, wraplength=900, foreground="#555"
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
                  foreground="#555").pack(anchor="w", padx=8)

        row = ttk.Frame(up)
        row.pack(fill="x", padx=8, pady=(6, 8))
        self.notify_var = tk.BooleanVar(value=bool(self.cfg.get("notify_subscribers", False)))
        ttk.Checkbutton(row, text="Notify subscribers on upload",
                        variable=self.notify_var).pack(side="left")
        self.kids_var = tk.BooleanVar(value=bool(self.cfg.get("made_for_kids", False)))
        ttk.Checkbutton(row, text="Mark as “made for kids”",
                        variable=self.kids_var).pack(side="left", padx=14)
        ttk.Label(row, text="Upload chunk size (MB):").pack(side="left", padx=(14, 0))
        self.chunk_var = tk.StringVar(value=str(self.cfg.get("chunk_mb", 8)))
        ttk.Entry(row, textvariable=self.chunk_var, width=5).pack(side="left", padx=4)

        ttk.Button(tab, text="Save settings", command=self.save_settings
                   ).pack(anchor="e", padx=10)

        notes = (
            "Quota note: every upload costs 1600 API units and Google's default daily quota "
            "is 10,000 units, i.e. about 6 uploads per day. The queue pauses automatically "
            "when quota runs out (it resets at midnight Pacific time).\n"
            "Important: while your Google Cloud OAuth app is unverified / in testing mode, "
            "videos uploaded through the API are locked to PRIVATE by YouTube. Complete the "
            "API audit/verification to allow public uploads.")
        ttk.Label(tab, text=notes, wraplength=940, foreground="#555", justify="left"
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
        if vod.key in self.registry:
            return "uploaded ✓", "uploaded"
        for item in self.queue_items:
            if item.key == vod.key and item.status in ("queued", "uploading"):
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
                values=(vod.date_str, vod.streamer_name or vod.streamer_login,
                        vod.stream_title, dur,
                        fmt_size(vod.size_bytes) if vod.size_bytes else "—",
                        len(vod.chapters), status))
        for iid in selected:
            if self.video_tree.exists(iid):
                self.video_tree.selection_add(iid)

    def _open_vod_folder(self, _event) -> None:
        sel = self.video_tree.selection()
        if sel and sel[0] in self.vods:
            os.startfile(self.vods[sel[0]].folder)  # noqa: S606

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
            text=f"{n}/100", foreground="#b71c1c" if n > 100 else "#000")

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

    def add_all_to_queue(self) -> None:
        self._save_editor()
        self._enqueue(list(self.vods))

    def _enqueue(self, keys: list[str]) -> None:
        added = skipped = 0
        queued_keys = {i.key for i in self.queue_items
                       if i.status in ("queued", "uploading", "done")}
        for key in keys:
            vod = self.vods[key]
            if key in self.registry or key in queued_keys:
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
        self.queue_tree.delete(*self.queue_tree.get_children())
        for idx, item in enumerate(self.queue_items, start=1):
            detail = item.detail
            if item.status == "uploading" and item.progress:
                detail = f"{item.progress:.1f}%  {item.detail}"
            tag = item.status if item.status in ("done", "error", "uploading") else ""
            self.queue_tree.insert(
                "", "end", iid=item.key, tags=(tag,) if tag else (),
                values=(idx, item.title, fmt_size(item.vod.size_bytes),
                        item.privacy, item.status, detail))
        pending = sum(1 for i in self.queue_items if i.status == "queued")
        done = sum(1 for i in self.queue_items if i.status == "done")
        self.status_queue.configure(
            text=f"Queue: {pending} pending, {done} done, {len(self.queue_items)} total")

    def _selected_queue_index(self) -> int | None:
        sel = self.queue_tree.selection()
        if not sel:
            return None
        for i, item in enumerate(self.queue_items):
            if item.key == sel[0]:
                return i
        return None

    def remove_queue_item(self) -> None:
        idx = self._selected_queue_index()
        if idx is None:
            return
        if self.queue_items[idx].status == "uploading":
            messagebox.showinfo("Queue", "That video is uploading — use "
                                "“Cancel current upload” first.")
            return
        del self.queue_items[idx]
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
            chunk_mb = max(1, int(self.chunk_var.get()))
        except ValueError:
            chunk_mb = 8
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
                self.registry[item.key] = {
                    "video_id": item.video_id,
                    "title": item.title,
                    "uploaded_at": datetime.now(timezone.utc).isoformat(),
                }
                config.save_registry(self.registry)
                self.progressbar.configure(value=0)
                self.current_label.configure(text="Idle.")
            if item and ev["status"] == "uploading":
                self.current_label.configure(text=f"Uploading: {item.title}")
                self.progressbar.configure(value=0)
            self._refresh_queue_tree()
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
