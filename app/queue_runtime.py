"""Shared multi-queue core.

One `QueueRuntime` per configured queue (its own VOD folder, YouTube
account, scan results, pending uploads, and cooldown). One `QueueManager`
owns all of them, persists `queues.json`, and runs the scheduler: only one
queue's `UploadWorker` is ever alive at a time (the "upload slot"); when it
stops — finished, paused, errored, or cooled down — the manager hands the
slot to the next queue that still wants to run, in rotation order, so one
channel's cooldown never blocks a different channel/folder's queue.

YouTube enforces two different limits (see app/limits.py): a channel's own
uploadLimitExceeded is tracked per-queue (each queue's own `queue_state_
<id>.json`); the API project's quotaExceeded/dailyLimitExceeded is shared by
every queue using the same OAuth client, so it is tracked once, here, in the
app-wide config — hitting it pauses every queue, not just the one that saw
the error.

Both the desktop GUI (app/gui.py) and the web/Docker app (web/state.py) own
one QueueManager and build their UI/HTTP layer on top of it; the scan/queue/
upload/cooldown logic itself lives only here.
"""
from __future__ import annotations

import os
import queue
import shutil
import stat
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from . import auth, config, limits, lsdvr, playlists as playlists_api, scanner
from .scanner import Vod
from .uploader import QueueItem, UploadWorker, verify_video

BACKGROUND_TICK_S = 1.0
CREDENTIAL_RETRY_TICKS = 10   # how often an idle queue's sign-in is retried


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _clear_readonly(path: Path) -> None:
    """Best-effort: drop the read-only attribute on path and everything
    under it. Some SMB/NAS shares mark individual files read-only in a way
    that makes both the Recycle Bin and a plain delete fail with Windows'
    "Access is denied" (WinError 5), even though the rest of the folder
    deletes fine."""
    try:
        if path.is_dir():
            for root, dirs, files in os.walk(path):
                for name in dirs + files:
                    try:
                        os.chmod(os.path.join(root, name), stat.S_IWRITE)
                    except OSError:
                        pass
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass


class QueueRuntime:
    """One queue's live state: scan results, metadata edits, pending upload
    items, worker thread, credentials, and its own channel-limit cooldown."""

    def __init__(self, manager: "QueueManager", queue_def: dict):
        self.manager = manager
        self.id: str = queue_def["id"]
        self.def_: dict = queue_def
        self.vods: dict[str, Vod] = {}
        self.metas: dict[str, dict] = {}
        self.queue_items: list[QueueItem] = []
        self.worker: UploadWorker | None = None
        self.worker_events: queue.Queue | None = None
        self.credentials = None
        self.channel: dict | None = None
        self.playlists: list[dict] = []
        self.playlist_ids: dict[str, str] = {}
        self.scan_state: dict = {"active": False, "done": 0, "total": 0,
                                 "current": "", "found": None, "error": ""}
        self.op_state: dict = {"active": False, "label": "", "done": 0,
                               "total": 0, "result": "", "ok_count": 0}
        self.lock = threading.RLock()
        self._state: dict = config.load_queue_state(self.id)
        self._queue_restored = False
        self._auto_countdown = int(self.setting("auto_scan_interval_min", 10)) * 60
        self.auto_status = "off"
        self._cred_tick = 0

    # -------------------------------------------------------------- settings
    def setting(self, key, default=None):
        if key in self.def_:
            return self.def_[key]
        if default is not None:
            return default
        return config.DEFAULTS.get(key)

    @property
    def name(self) -> str:
        return self.def_.get("name") or self.id

    @property
    def vod_folder(self) -> str:
        return self.def_.get("vod_folder", "")

    @property
    def account_id(self) -> str:
        return self.def_.get("account_id", "")

    @property
    def enabled(self) -> bool:
        return bool(self.def_.get("enabled", True))

    def log(self, text: str) -> None:
        self.manager.log(f"[{self.name}] {text}")

    def emit(self, event: dict) -> None:
        event = dict(event)
        event["queue_id"] = self.id
        self.manager.emit_ui_event(event)

    # ------------------------------------------------------------------ auth
    def ensure_credentials(self) -> bool:
        """Loads (and caches) this queue's own account credentials. Safe to
        call repeatedly — it's a no-op once `self.credentials` is set."""
        if self.credentials is not None:
            return True
        if not self.account_id:
            return False
        creds = auth.load_account(self.account_id)
        if creds is None:
            return False
        self.credentials = creds
        try:
            self.channel = auth.fetch_channel(auth.build_service(creds))
        except Exception as exc:
            self.channel = None
            self.log("signed in, but the channel lookup failed: "
                     + auth.describe_api_error(exc))
        self.refresh_playlists()
        return True

    def forget_credentials(self) -> None:
        self.credentials = None
        self.channel = None
        self.playlists = []
        self.playlist_ids = {}

    def refresh_playlists(self) -> None:
        if self.credentials is None:
            return

        def worker():
            try:
                service = auth.build_service(self.credentials)
                items = playlists_api.list_playlists(service)
                with self.lock:
                    self.playlists = items
                    self.playlist_ids = {p["title"]: p["id"] for p in items}
                self.emit({"type": "playlists", "items": items})
            except Exception as exc:
                self.log("could not load playlists: " + auth.describe_api_error(exc))

        threading.Thread(target=worker, daemon=True).start()

    def create_playlist(self, title: str, privacy: str) -> None:
        if self.credentials is None:
            raise RuntimeError("not signed in")

        def worker():
            try:
                service = auth.build_service(self.credentials)
                playlists_api.create_playlist(service, title, privacy)
                self.log(f"created playlist '{title}' ({privacy})")
                self.refresh_playlists()
            except Exception as exc:
                self.log("could not create playlist: " + auth.describe_api_error(exc))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ scan
    def scan(self, folder: str | None = None) -> int:
        """Blocking scan; live progress is published via self.scan_state
        (polled) and a scan_progress/scan_done ui event (for the desktop
        GUI's live label)."""
        folder = (folder or self.vod_folder or "").strip()
        root = Path(folder)
        if not root.is_dir():
            self.scan_state.update(active=False, found=None,
                                   error=f"folder does not exist: {folder}")
            raise FileNotFoundError(f"folder does not exist: {folder}")
        if self.scan_state.get("active"):
            raise RuntimeError("a scan is already running")
        if folder != self.vod_folder:
            self.def_["vod_folder"] = folder
            self.manager.save_queues()
        self.scan_state.update(active=True, done=0, total=0, current="",
                               found=None, error="")

        def progress(done, total, name):
            self.scan_state.update(done=done, total=total, current=name)
            self.emit({"type": "scan_progress", "done": done, "total": total,
                       "name": name})

        try:
            vods = scanner.scan_folder(root, progress=progress)
        except Exception as exc:
            self.scan_state.update(active=False, error=str(exc)[:200])
            raise
        with self.lock:
            self.vods = {v.key: v for v in vods}
            for key in list(self.metas):
                if key not in self.vods:
                    del self.metas[key]
            for vod in vods:
                if vod.key not in self.metas:
                    self.metas[vod.key] = self._generate_meta(vod)
            if not self._queue_restored:
                self._restore_queue_state()
        self.scan_state.update(active=False, found=len(vods))
        self.log(f"scanned {folder}: found {len(vods)} VOD folder(s)."
                 if vods else f"scanned {folder}: no VOD folders found.")
        self.emit({"type": "scan_done", "found": len(vods)})
        return len(vods)

    def scan_async(self, folder: str | None = None) -> None:
        if self.scan_state.get("active"):
            raise RuntimeError("a scan is already running")

        def worker():
            try:
                self.scan(folder)
            except (FileNotFoundError, RuntimeError) as exc:
                self.log(f"scan failed: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def _generate_meta(self, vod: Vod) -> dict:
        tags = scanner.build_tags(vod)
        seen = {t.lower() for t in tags}
        for tag in (t.strip() for t in self.setting("extra_tags", "").split(",")):
            if tag and tag.lower() not in seen:
                tags.append(tag)
                seen.add(tag.lower())
        return {
            "title": scanner.build_title(vod, self.setting("title_template")),
            "description": scanner.build_description(
                vod, self.setting("description_template") or None),
            "tags": ", ".join(tags),
            "privacy": self.setting("privacy"),
            "playlist_choice": "(default)",
        }

    def vod_status(self, vod: Vod) -> str:
        entry = self.manager.registry.get(vod.key)
        if entry:
            if entry.get("failed"):
                return "failed on YouTube"
            if entry.get("local_deleted"):
                return "uploaded ✓ · cleaned"
            if entry.get("verified"):
                return "uploaded ✓ verified"
            return "uploaded (unverified)"
        for item in self.queue_items:
            if item.key == vod.key and item.status in ("queued", "uploading", "verifying"):
                return item.status
        if vod.problems:
            return ", ".join(vod.problems)
        return "ready"

    def update_meta(self, key: str, patch: dict) -> None:
        with self.lock:
            if key not in self.metas:
                raise KeyError(key)
            self.metas[key].update({k: v for k, v in patch.items() if v is not None})
        self._save_queue_state()

    # -------------------------------------------------------------- playlists
    def _render_playlist_name(self, vod: Vod | None) -> str:
        if vod is None:
            return ""
        template = self.setting("playlist_template") or "{streamer} VODs"
        values = _SafeDict(
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

    def _resolve_playlist_spec(self, key: str) -> dict | None:
        choice = self.metas.get(key, {}).get("playlist_choice", "(default)")
        if choice == "(none)":
            return None
        if choice != "(default)":
            pid = self.playlist_ids.get(choice)
            if pid:
                return {"type": "id", "value": pid, "title": choice}
            return {"type": "name", "value": choice}
        mode = self.setting("playlist_mode", "none")
        if mode == "fixed" and self.setting("playlist_fixed_id"):
            return {"type": "id", "value": self.setting("playlist_fixed_id"),
                    "title": self.setting("playlist_fixed_title", "")}
        if mode == "template":
            name = self._render_playlist_name(self.vods.get(key))
            if name:
                return {"type": "name", "value": name}
        return None

    # ------------------------------------------------------------------ queue
    def _save_queue_state(self) -> None:
        """Persist pending items + edited metadata + this queue's own
        cooldown/running-intent. Deliberately not called on every 'progress'
        tick — see callers."""
        with self.lock:
            queue_data = [{
                "vod_key": item.key, "title": item.title,
                "description": item.description, "tags": item.tags,
                "privacy": item.privacy, "category_id": item.category_id,
                "recording_date": item.recording_date,
                "notify_subscribers": item.notify_subscribers,
                "made_for_kids": item.made_for_kids, "playlist": item.playlist,
                "status": item.status, "detail": item.detail,
                "video_id": item.video_id, "progress": item.progress,
            } for item in self.queue_items]
            self._state["queue"] = queue_data
            self._state["metas"] = self.metas
            try:
                config.save_queue_state(self.id, self._state)
            except OSError as exc:
                self.log(f"could not save queue state: {exc}")

    def _restore_queue_state(self) -> None:
        """Called once, right after this queue's first scan, so restored
        entries can be matched against freshly scanned Vod objects."""
        self._queue_restored = True
        saved = self._state
        items: list[QueueItem] = []
        dropped = 0
        for entry in saved.get("queue", []):
            vod = self.vods.get(entry.get("vod_key", ""))
            status = entry.get("status", "queued")
            if vod is None:
                if status not in ("done", "cancelled"):
                    dropped += 1
                continue
            progress = float(entry.get("progress", 0.0) or 0.0)
            detail = entry.get("detail", "")
            if status in ("uploading", "verifying"):
                # No byte-level resume across a restart — it starts over.
                status, progress = "queued", 0.0
                detail = "interrupted by a restart — will start over"
            items.append(QueueItem(
                vod=vod, title=entry.get("title") or vod.stream_title,
                description=entry.get("description", ""),
                tags=list(entry.get("tags") or []),
                privacy=entry.get("privacy", self.setting("privacy")),
                category_id=str(entry.get("category_id")
                                or self.setting("category_id", "20")),
                recording_date=entry.get("recording_date"),
                notify_subscribers=bool(entry.get("notify_subscribers", False)),
                made_for_kids=bool(entry.get("made_for_kids", False)),
                playlist=entry.get("playlist"),
                status=status, detail=detail,
                video_id=entry.get("video_id"), progress=progress,
            ))
        for key, meta in saved.get("metas", {}).items():
            if key in self.metas:
                self.metas[key] = meta
        if dropped:
            self.log(f"dropped {dropped} queued upload(s) whose VOD folder "
                     "no longer exists.")
        if not items:
            return
        self.queue_items = items
        pending = sum(1 for i in items if i.status == "queued")
        self.log(f"restored {len(items)} queue item(s) from before the restart "
                 f"({pending} still to upload).")

    def enqueue(self, keys: list[str]) -> tuple[int, int]:
        added = skipped = 0
        with self.lock:
            busy = {i.key for i in self.queue_items
                    if i.status in ("queued", "uploading", "verifying", "done")}
            for key in keys:
                vod = self.vods.get(key)
                if vod is None:
                    continue
                entry = self.manager.registry.get(key)
                if (entry and not entry.get("failed")) or key in busy \
                        or vod.video_path is None:
                    skipped += 1
                    continue
                meta = self.metas[key]
                self.queue_items.append(QueueItem(
                    vod=vod, title=scanner.sanitize_title(meta["title"]),
                    description=meta["description"],
                    tags=[t.strip() for t in meta["tags"].split(",") if t.strip()],
                    privacy=meta["privacy"],
                    category_id=str(self.setting("category_id", "20")),
                    recording_date=scanner.recording_date(vod),
                    notify_subscribers=bool(self.setting("notify_subscribers", False)),
                    made_for_kids=bool(self.setting("made_for_kids", False)),
                    playlist=self._resolve_playlist_spec(key),
                ))
                added += 1
        if added:
            self._save_queue_state()
        if added or skipped:
            self.log(f"queued {added} video(s)"
                     + (f", skipped {skipped}." if skipped else "."))
        return added, skipped

    def remove_from_queue(self, keys: list[str]) -> None:
        with self.lock:
            self.queue_items = [
                i for i in self.queue_items
                if i.key not in keys or i.status in ("uploading", "verifying")]
        self._save_queue_state()

    def move_in_queue(self, key: str, delta: int) -> None:
        with self.lock:
            idx = next((i for i, item in enumerate(self.queue_items)
                        if item.key == key), None)
            if idx is None:
                return
            new = max(0, min(len(self.queue_items) - 1, idx + delta))
            item = self.queue_items.pop(idx)
            self.queue_items.insert(new, item)
        self._save_queue_state()

    def clear_finished(self) -> None:
        with self.lock:
            self.queue_items = [i for i in self.queue_items
                                if i.status not in ("done", "cancelled")]
        self._save_queue_state()

    def retry_failed(self) -> int:
        count = 0
        with self.lock:
            for item in self.queue_items:
                if item.status in ("error", "cancelled"):
                    item.status = "queued"
                    item.detail = ""
                    item.progress = 0.0
                    count += 1
        if count:
            self._save_queue_state()
        self.log(f"re-queued {count} failed upload(s)." if count
                 else "no failed uploads in the queue.")
        return count

    # ---------------------------------------------------------------- uploads
    def wants_to_run(self) -> bool:
        """Read-only eligibility check used by the manager's rotation: has
        pending items, was told to run (persisted — survives cooldowns and
        restarts), isn't already running, and neither cooldown is active."""
        if not self.enabled:
            return False
        if self.worker and self.worker.is_alive():
            return False
        if not self._state.get("running", False):
            return False
        if not any(i.status == "queued" for i in self.queue_items):
            return False
        if limits.get_cooldown(self._state) is not None:
            return False
        if limits.get_quota_cooldown(self.manager.cfg) is not None:
            return False
        return self.credentials is not None

    def start_uploads(self) -> None:
        """Actually spins up the UploadWorker. Only the manager calls this —
        it's the sole place enforcing the "one upload at a time" rule and
        the running/cooldown bookkeeping that gates this method."""
        self.worker_events = queue.Queue()
        self.worker = UploadWorker(
            self.credentials, self.queue_items, self.worker_events,
            daily_limit=int(self.setting("daily_upload_limit", 0) or 0),
            count_recent=lambda: limits.count_recent(self.manager.registry, self.id),
            speed_limit_bps=float(self.setting("upload_speed_limit", 0) or 0) * 1e6,
            verify=bool(self.setting("verify_uploads", True)))
        self.worker.start()
        threading.Thread(target=self._pump_worker, args=(self.worker_events,),
                         daemon=True).start()
        self._state["running"] = True
        self._save_queue_state()
        self.log("upload queue started.")
        self.emit({"type": "queue_started"})

    def pause_uploads(self) -> None:
        if self.worker:
            self.worker.pause_requested.set()
            self.log("will pause after the current upload.")

    def cancel_current(self) -> None:
        if self.worker:
            self.worker.cancel_current.set()
            self.log("cancelling current upload…")

    def _pump_worker(self, events: queue.Queue) -> None:
        while True:
            try:
                ev = events.get(timeout=1.0)
            except queue.Empty:
                if not (self.worker and self.worker.is_alive()):
                    return
                continue
            etype = ev.get("type")
            if etype == "log":
                self.log(ev["text"])
            elif etype == "item_status" and ev["status"] == "done":
                verified = bool(ev.get("verified"))
                key = ev["key"]
                with self.lock:
                    item = next((i for i in self.queue_items if i.key == key), None)
                    self.manager.registry[key] = {
                        "video_id": ev.get("video_id"),
                        "title": item.title if item else "",
                        "folder": str(item.vod.folder) if item else "",
                        "uploaded_at": datetime.now(timezone.utc).isoformat(),
                        "verified": verified,
                        "queue_id": self.id,
                    }
                    self.manager.save_registry()
                mode = self.setting("after_upload", "keep")
                if mode == "keep":
                    self._record_local(key, "kept",
                                       "after-upload setting is “keep”")
                elif not verified:
                    self._record_local(key, "kept",
                                       "not deleted — the upload could not be "
                                       "verified on YouTube")
                    self.log(f"kept local files of {key}: upload not verified.")
                else:
                    self._dispose_vod(key, mode)
                self._save_queue_state()
                self.emit(ev)
            elif etype in ("item_status", "item_detail"):
                self._save_queue_state()
                self.emit(ev)
            elif etype == "progress":
                with self.lock:
                    for item in self.queue_items:
                        if item.key == ev["key"]:
                            item.progress = ev["pct"]
                            if ev.get("speed_bps"):
                                item.detail = (f"{ev['speed_bps'] / 1e6:.1f} MB/s, "
                                               f"ETA {int(ev['eta_s'])}s")
                self.emit(ev)
            elif etype == "worker_done":
                self._on_worker_done(ev)
                self.emit(ev)
                self.manager.on_worker_done(self.id)

    def _on_worker_done(self, ev: dict) -> None:
        reason = ev.get("reason", "")
        if reason in ("finished", "paused", "error"):
            self._state["running"] = False
        detail = ev.get("detail", "")
        if reason == "quota":
            if "uploadLimitExceeded" in detail:
                until = limits.quota_cooldown(detail, self.setting("cooldown_hours"))
                limits.set_cooldown(self._state, until, detail or "limit")
                self.log(f"channel upload limit hit — this queue cools down "
                         f"until {limits.fmt_local(until)}; other queues are "
                         "unaffected.")
            else:
                until = limits.quota_cooldown(detail, self.setting("cooldown_hours"))
                with self.manager.lock:
                    limits.set_quota_cooldown(self.manager.cfg, until, detail or "limit")
                    config.save_config(self.manager.cfg)
                self.log("shared YouTube API quota hit — every queue cools "
                         f"down until {limits.fmt_local(until)}.")
        elif reason == "daily_limit":
            until = limits.next_slot(
                self.manager.registry,
                int(self.setting("daily_upload_limit", 0) or 0), self.id)
            limits.set_cooldown(self._state, until, "daily upload limit")
            self.log(f"daily limit reached — next upload at {limits.fmt_local(until)}.")
        else:
            self.log({"finished": "queue finished.",
                     "paused": "queue paused."}.get(reason, "idle."))
        self._save_queue_state()

    # ---------------------------------------------------------- housekeeping
    def _record_local(self, key: str, state: str, detail: str, path: str = "") -> None:
        with self.lock:
            entry = self.manager.registry.get(key)
            if entry is None:
                return
            entry["local"] = {"state": state, "detail": detail, "path": path,
                              "at": datetime.now(timezone.utc).isoformat()}
            self.manager.save_registry()

    def _dispose_vod(self, key: str, mode: str) -> bool:
        if self.setting("lsdvr_delete"):
            return self._dispose_vod_lsdvr(key, mode)
        return self._dispose_vod_local(key, mode)

    def _dispose_vod_local(self, key: str, mode: str) -> bool:
        what = "folder" if mode == "trash_folder" else "video file"
        vod = self.vods.get(key)
        if vod is None:
            self.log(f"{key}: no longer in the scanned list — {what} not "
                     "deleted by this app.")
            self._record_local(key, "missing",
                               f"VOD no longer in the scanned list — {what} "
                               "not deleted by this app")
            return False
        target = vod.folder if mode == "trash_folder" else vod.video_path
        if target is None or not target.exists():
            self.log(f"{key}: {what} was already gone before deletion.")
            self._record_local(key, "missing",
                               f"{what} was already gone before deletion",
                               str(target or ""))
            return False
        _clear_readonly(target)
        recycled = False
        stuck: list[str] = []
        if self.manager.recycle_bin_delete is not None:
            try:
                recycled = self.manager.recycle_bin_delete(target)
            except Exception as exc:
                self.log(f"could not recycle {target}: {exc}")
        if not recycled:
            if self.manager.recycle_bin_delete is not None \
                    and not self.setting("recycle_fallback_delete"):
                self.log(f"could not recycle {target} (Recycle Bin "
                         "unavailable, e.g. a network drive) and the "
                         "permanent-delete fallback isn't enabled.")
                self._record_local(key, "failed",
                                   f"could not recycle {what} (Recycle Bin "
                                   "unavailable, e.g. a network drive) and "
                                   "permanent delete is not enabled",
                                   str(target))
                return False
            try:
                if target.is_dir():
                    def _on_rm_error(func, p, exc):
                        stuck.append(p)
                        self.log(f"could not remove {p}: {exc}")
                    try:
                        shutil.rmtree(target, onexc=_on_rm_error)
                    except TypeError:
                        # Python < 3.12 has no onexc, only the older onerror
                        shutil.rmtree(
                            target,
                            onerror=lambda f, p, ei: _on_rm_error(f, p, ei[1]))
                else:
                    target.unlink()
            except OSError as exc:
                self.log(f"could not delete {target}: {exc}")
                self._record_local(key, "failed", f"could not delete {what}: {exc}",
                                   str(target))
                return False
        if target.exists():
            if mode == "trash_folder" and stuck:
                # best-effort delete got everything except file(s) it
                # genuinely couldn't remove (commonly a permission mismatch
                # on the share itself, not something a retry here fixes) —
                # treat as done rather than blocking on a single leftover
                self.log(f"deleted most of {target}; some file(s) inside it "
                         "could not be removed (see above) — the folder "
                         "stays with just those leftovers.")
            else:
                self.log(f"deleted {target}, but it is still on disk.")
                self._record_local(key, "failed",
                                   f"{what} still exists after deleting it", str(target))
                return False
        self._record_local(key, "deleted", f"{what} deleted", str(target))
        with self.lock:
            entry = self.manager.registry.setdefault(key, {})
            entry["local_deleted"] = True
            self.manager.save_registry()
            if mode == "trash_folder":
                self.vods.pop(key, None)
                self.metas.pop(key, None)
        self.log(f"deleted local files: {target}")
        return True

    def _dispose_vod_lsdvr(self, key: str, mode: str) -> bool:
        what = "folder" if mode == "trash_folder" else "video file"
        vod = self.vods.get(key)
        if vod is None:
            self.log(f"{key}: no longer in the scanned list — {what} not "
                     "deleted by this app.")
            self._record_local(key, "missing",
                               f"VOD no longer in the scanned list — {what} "
                               "not deleted by this app")
            return False
        video = vod.video_path
        if not vod.json_path.exists() or (mode != "trash_folder" and (
                video is None or not video.exists())):
            self.log(f"{key}: {what} was already gone before deletion.")
            self._record_local(key, "missing",
                               f"{what} was already gone before deletion",
                               str(video or vod.folder))
            return False
        try:
            info = lsdvr.read_vod_info(vod.json_path)
        except (OSError, ValueError) as exc:
            self.log(f"{key}: could not read LiveStreamDVR's VOD file "
                     f"({vod.json_path.name}): {exc}")
            self._record_local(key, "failed", "could not read LiveStreamDVR's "
                               f"VOD file: {exc}", str(vod.json_path))
            return False
        vod_uuid = info.get("uuid")
        busy = [word for flag, word in (("is_capturing", "recording"),
                                        ("is_converting", "converting"))
                if info.get(flag)]
        if not vod_uuid:
            reason = "its VOD file has no uuid (made by an old LiveStreamDVR?)"
        elif busy:
            reason = f"LiveStreamDVR is still {' and '.join(busy)} it"
        elif info.get("prevent_deletion"):
            reason = "it is marked “prevent deletion” in LiveStreamDVR"
        else:
            reason = ""
        if reason:
            self.log(f"kept local files of {key}: {reason}.")
            self._record_local(key, "kept", f"not deleted — {reason}", str(vod.folder))
            return False
        try:
            client = lsdvr.Client(self.manager.cfg.get("lsdvr_url", ""),
                                  self.manager.cfg.get("lsdvr_password", ""))
            if mode == "trash_folder":
                client.delete_vod(vod_uuid)
            else:
                client.delete_segment(vod_uuid, lsdvr.segment_index(info, video))
        except lsdvr.LsdvrError as exc:
            if "not found" in str(exc).lower():
                # LiveStreamDVR has no database record of this VOD at all, so
                # it isn't watching/managing it either — deleting the leftover
                # files ourselves can't trigger the crash this feature exists
                # to avoid.
                self.log(f"LiveStreamDVR has no record of {key} anymore "
                         "(already gone from its database) — deleting the "
                         "leftover files directly instead.")
                return self._dispose_vod_local(key, mode)
            self.log(f"LiveStreamDVR could not delete {key}: {exc}")
            self._record_local(key, "failed", f"LiveStreamDVR: {exc}", str(vod.folder))
            return False
        if mode == "trash_folder":
            left = [p for p in (vod.json_path, video) if p and p.exists()]
        else:
            left = [video] if video and video.exists() else []
        if left:
            self.log(f"LiveStreamDVR deleted {key}, but {left[0].name} is "
                     "still on disk.")
            self._record_local(key, "failed", "LiveStreamDVR reported success "
                               f"but {left[0].name} is still on disk", str(left[0]))
            return False
        detail = f"{what} deleted by LiveStreamDVR"
        path = vod.folder if mode == "trash_folder" else video
        if mode == "trash_folder" and vod.folder.exists():
            extra = sum(1 for _ in vod.folder.iterdir())
            detail += f"; the folder stays with {extra} unrelated file(s)"
            path = video or vod.json_path
        self._record_local(key, "deleted", detail, str(path))
        with self.lock:
            entry = self.manager.registry.setdefault(key, {})
            entry["local_deleted"] = True
            self.manager.save_registry()
            if mode == "trash_folder":
                self.vods.pop(key, None)
                self.metas.pop(key, None)
        self.log(f"LiveStreamDVR deleted the {what} of {key}.")
        return True

    def bulk(self, action: str, keys: list[str], value: str = "") -> dict:
        keys = [k for k in keys if k in self.vods]
        if action == "queue":
            added, skipped = self.enqueue(keys)
            return {"added": added, "skipped": skipped}
        if action == "reset_meta":
            with self.lock:
                for key in keys:
                    self.metas[key] = self._generate_meta(self.vods[key])
            self._save_queue_state()
            self.log(f"reset metadata for {len(keys)} video(s).")
            return {"count": len(keys)}
        if action == "privacy":
            with self.lock:
                for key in keys:
                    self.metas[key]["privacy"] = value
            self._save_queue_state()
            self.log(f"set privacy '{value}' on {len(keys)} video(s).")
            return {"count": len(keys)}
        if action == "playlist":
            with self.lock:
                for key in keys:
                    self.metas[key]["playlist_choice"] = value or "(default)"
            self._save_queue_state()
            self.log(f"set playlist '{value}' on {len(keys)} video(s).")
            return {"count": len(keys)}
        if action == "reset_state":
            with self.lock:
                cleared = [k for k in keys
                          if self.manager.registry.pop(k, None) is not None]
                self.manager.save_registry()
            self.log(f"reset upload state of {len(cleared)} video(s).")
            return {"count": len(cleared)}
        if action == "verify":
            targets = [k for k in keys
                      if self.manager.registry.get(k, {}).get("video_id")]
            if not targets:
                raise RuntimeError("none of these were uploaded yet")
            if self.credentials is None:
                raise RuntimeError("not signed in")
            threading.Thread(target=self._verify_worker, args=(targets,),
                             daemon=True).start()
            return {"count": len(targets)}
        if action == "delete_local":
            candidates = [
                key for key in keys
                if (entry := self.manager.registry.get(key))
                and entry.get("verified") and not entry.get("local_deleted")]
            if not candidates:
                raise RuntimeError("nothing deletable — only uploaded AND "
                                   "verified videos can be deleted")
            if self.op_state.get("active"):
                raise RuntimeError("another delete operation is running")
            self.op_state.update(active=True, label="Deleting", done=0,
                                 total=len(candidates), result="", ok_count=0)

            def worker():
                done = 0
                for i, key in enumerate(candidates, start=1):
                    self.op_state.update(done=i)
                    if self._dispose_vod(key, "trash_folder"):
                        done += 1
                self.op_state.update(
                    active=False, ok_count=done,
                    result=f"Deleted {done} of {len(candidates)} folder(s)")
                self.log(f"deleted {done} of {len(candidates)} VOD folder(s).")
                try:
                    self.scan()
                except (FileNotFoundError, RuntimeError):
                    pass

            threading.Thread(target=worker, daemon=True).start()
            return {"started": True, "count": len(candidates)}
        if action == "delete_local_any":
            # Desktop's "delete local (any status)" — no verified-upload
            # requirement, for cleaning up duplicates/test recordings;
            # works even on videos that were never uploaded at all.
            candidates = [key for key in keys
                         if not (self.manager.registry.get(key) or {}).get("local_deleted")]
            if not candidates:
                raise RuntimeError("nothing to delete — already removed")
            if self.op_state.get("active"):
                raise RuntimeError("another delete operation is running")
            self.op_state.update(active=True, label="Deleting", done=0,
                                 total=len(candidates), result="", ok_count=0)

            def worker():
                done = 0
                for i, key in enumerate(candidates, start=1):
                    self.op_state.update(done=i)
                    if self._dispose_vod(key, "trash_folder"):
                        done += 1
                self.op_state.update(
                    active=False, ok_count=done,
                    result=f"Deleted {done} of {len(candidates)} folder(s)")
                self.log(f"deleted {done} of {len(candidates)} VOD folder(s) "
                         "(any status).")
                try:
                    self.scan()
                except (FileNotFoundError, RuntimeError):
                    pass

            threading.Thread(target=worker, daemon=True).start()
            return {"started": True, "count": len(candidates)}
        if action == "delete_local_force":
            # Rescue action: skip LiveStreamDVR entirely and delete the local
            # files directly, even when this queue has "Delete through the
            # LiveStreamDVR API" turned on — for when LiveStreamDVR itself
            # can't do it (unreachable, its own server out of disk space,
            # ...). The UI confirmation warns this can crash LiveStreamDVR if
            # it's still actively watching the folder.
            candidates = [key for key in keys
                         if not (self.manager.registry.get(key) or {}).get("local_deleted")]
            if not candidates:
                raise RuntimeError("nothing to delete — already removed")
            if self.op_state.get("active"):
                raise RuntimeError("another delete operation is running")
            self.op_state.update(active=True, label="Deleting", done=0,
                                 total=len(candidates), result="", ok_count=0)

            def worker():
                done = 0
                for i, key in enumerate(candidates, start=1):
                    self.op_state.update(done=i)
                    if self._dispose_vod_local(key, "trash_folder"):
                        done += 1
                self.op_state.update(
                    active=False, ok_count=done,
                    result=f"Deleted {done} of {len(candidates)} folder(s)")
                self.log(f"deleted {done} of {len(candidates)} VOD folder(s) "
                         "(forced local, bypassing LiveStreamDVR).")
                try:
                    self.scan()
                except (FileNotFoundError, RuntimeError):
                    pass

            threading.Thread(target=worker, daemon=True).start()
            return {"started": True, "count": len(candidates)}
        raise RuntimeError(f"unknown bulk action: {action}")

    def history(self) -> list[dict]:
        with self.lock:
            entries = {k: dict(v) for k, v in self.manager.registry.items()
                      if v.get("queue_id") == self.id}
            root = Path(self.vod_folder)
            video_paths = {k: v.video_path for k, v in self.vods.items()}
        rows = []
        for key, entry in entries.items():
            folder = Path(entry.get("folder") or (root / key))
            folder_exists = folder.exists()
            video = video_paths.get(key)
            video_exists = bool(folder_exists and video and video.exists())
            local = entry.get("local") or {}
            state = local.get("state", "")
            if state == "deleted":
                path = local.get("path")
                still = Path(path).exists() if path else folder_exists
                check = "still_on_disk" if still else "gone"
            elif state:
                check = state
            elif entry.get("local_deleted"):
                check = "gone" if not folder_exists else "legacy"
            else:
                check = "legacy"
            rows.append({
                "key": key, "title": entry.get("title", ""),
                "video_id": entry.get("video_id"),
                "uploaded_at": entry.get("uploaded_at", ""),
                "verified": bool(entry.get("verified")),
                "failed": bool(entry.get("failed")),
                "local": local, "check": check, "folder": str(folder),
                "folder_exists": folder_exists, "video_exists": video_exists,
            })
        rows.sort(key=lambda r: r["uploaded_at"] or "", reverse=True)
        return rows

    def _verify_worker(self, keys: list[str]) -> None:
        try:
            service = auth.build_service(self.credentials)
        except Exception as exc:
            self.log(f"verify failed: {exc}")
            return
        for key in keys:
            video_id = self.manager.registry[key]["video_id"]
            try:
                ok, detail = verify_video(service, video_id)
            except Exception as exc:
                ok, detail = None, str(exc)[:200]
            with self.lock:
                entry = self.manager.registry.get(key)
                if entry is not None and ok is not None:
                    entry["verified"] = bool(ok)
                    entry["failed"] = not ok
                    entry["verify_detail"] = detail
                    self.manager.save_registry()
            state = {True: "OK", False: "MISSING/FAILED",
                     None: "check unavailable"}[ok]
            self.log(f"verify {key} (youtu.be/{video_id}): {state} — {detail}")
            self.emit({"type": "verify_result", "key": key, "ok": ok,
                       "detail": detail, "video_id": video_id})

    # ------------------------------------------------------------- serialize
    def snapshot(self) -> dict:
        with self.lock:
            vods = []
            for vod in self.vods.values():
                meta = self.metas.get(vod.key, {})
                vods.append({
                    "key": vod.key, "date": vod.date_str,
                    "streamer": vod.streamer_name or vod.streamer_login,
                    "stream_title": vod.stream_title, "duration": vod.duration,
                    "size": vod.size_bytes, "chapters": len(vod.chapters),
                    "problems": vod.problems, "status": self.vod_status(vod),
                    "meta": meta,
                })
            queue_rows = [{
                "key": i.key, "title": i.title, "size": i.vod.size_bytes,
                "privacy": i.privacy, "status": i.status, "detail": i.detail,
                "progress": round(i.progress, 1), "video_id": i.video_id,
                "local": (self.manager.registry.get(i.key) or {}).get("local")
                if i.status == "done" else None,
            } for i in self.queue_items]
            cooldown = limits.get_cooldown(self._state)
            return {
                "id": self.id, "name": self.name, "enabled": self.enabled,
                "order": self.def_.get("order", 0),
                "vod_folder": self.vod_folder, "account_id": self.account_id,
                "settings": {k: self.setting(k) for k in config.QUEUE_SETTINGS_KEYS},
                "scan": dict(self.scan_state),
                "op": dict(self.op_state),
                "cooldown": limits.fmt_local(cooldown) if cooldown else None,
                "cooldown_reason": self._state.get("cooldown_reason", ""),
                "uploads_last_24h": limits.count_recent(self.manager.registry, self.id),
                "channel": self.channel["title"] if self.channel else None,
                "signed_in": self.credentials is not None,
                "vods": vods,
                "queue": queue_rows,
                "uploading": bool(self.worker and self.worker.is_alive()),
                "wants_running": bool(self._state.get("running")),
                "playlists": self.playlists,
                "auto_status": self.auto_status,
            }


class QueueManager:
    """Owns every QueueRuntime plus the resources they share: the upload
    registry (uploads.json), the app-wide config (for the shared API-quota
    cooldown and app-level settings like LiveStreamDVR), and the rotation
    scheduler that hands the single "upload slot" between queues."""

    def __init__(self, cfg: dict | None = None,
                 recycle_bin_delete=None):
        self.cfg = cfg if cfg is not None else config.load_config()
        migrated = config.migrate_legacy_queue(self.cfg)
        if migrated is not None:
            config.save_config(self.cfg)
        self.registry: dict = config.load_registry()
        self.lock = threading.RLock()
        self.events: deque = deque(maxlen=800)
        self._seq = 0
        self.ui_events: queue.Queue = queue.Queue()
        self.runtimes: dict[str, QueueRuntime] = {}
        self._active_queue_id: str | None = None
        # Desktop passes send2trash.send2trash (Recycle Bin); the web/Docker
        # app has none, so it always deletes permanently (no Recycle Bin on
        # a headless container).
        self.recycle_bin_delete = recycle_bin_delete
        self._tick_count = 0

        defs = migrated if migrated is not None else config.load_queues()
        for qdef in defs:
            self._add_runtime(qdef)
        for rt in self.runtimes.values():
            threading.Thread(target=rt.ensure_credentials, daemon=True).start()
            if Path(rt.vod_folder).is_dir():
                rt.scan_async()
        threading.Thread(target=self._background_loop, daemon=True).start()

    def _add_runtime(self, qdef: dict) -> QueueRuntime:
        rt = QueueRuntime(self, qdef)
        self.runtimes[rt.id] = rt
        return rt

    # --------------------------------------------------------------- CRUD
    def list_queues(self) -> list[QueueRuntime]:
        return sorted(self.runtimes.values(), key=lambda r: r.def_.get("order", 0))

    def get(self, queue_id: str) -> QueueRuntime:
        rt = self.runtimes.get(queue_id)
        if rt is None:
            raise KeyError(f"no such queue: {queue_id}")
        return rt

    def create_queue(self, name: str, vod_folder: str, account_id: str = "",
                      base: dict | None = None) -> QueueRuntime:
        qdef = {
            "id": uuid.uuid4().hex[:12],
            "name": name or "New queue",
            "vod_folder": vod_folder,
            "account_id": account_id,
            "enabled": True,
            "order": len(self.runtimes),
        }
        source = base or self.cfg
        for key in config.QUEUE_SETTINGS_KEYS:
            qdef[key] = source.get(key, config.DEFAULTS.get(key))
        rt = self._add_runtime(qdef)
        self.save_queues()
        self.log(f"created queue '{qdef['name']}'.")
        threading.Thread(target=rt.ensure_credentials, daemon=True).start()
        if vod_folder and Path(vod_folder).is_dir():
            rt.scan_async()
        return rt

    def update_queue(self, queue_id: str, patch: dict) -> QueueRuntime:
        rt = self.get(queue_id)
        with self.lock:
            account_changed = "account_id" in patch and patch["account_id"] != rt.account_id
            rt.def_.update(patch)
        self.save_queues()
        if account_changed:
            rt.forget_credentials()
            threading.Thread(target=rt.ensure_credentials, daemon=True).start()
        return rt

    def remove_queue(self, queue_id: str) -> None:
        rt = self.get(queue_id)
        if rt.worker and rt.worker.is_alive():
            raise RuntimeError("this queue is uploading — pause or cancel first")
        with self.lock:
            del self.runtimes[queue_id]
            if self._active_queue_id == queue_id:
                self._active_queue_id = None
        self.save_queues()
        try:
            config.queue_state_path(queue_id).unlink(missing_ok=True)
        except OSError:
            pass
        self.log(f"removed queue '{rt.name}'.")

    def reorder_queues(self, ordered_ids: list[str]) -> None:
        with self.lock:
            for i, qid in enumerate(ordered_ids):
                if qid in self.runtimes:
                    self.runtimes[qid].def_["order"] = i
        self.save_queues()

    def save_queues(self) -> None:
        config.save_queues([rt.def_ for rt in self.list_queues()])

    def save_registry(self) -> None:
        config.save_registry(self.registry)

    # ------------------------------------------------------------------ log
    def log(self, text: str) -> None:
        with self.lock:
            self._seq += 1
            self.events.append({"seq": self._seq,
                                "time": datetime.now().strftime("%H:%M:%S"),
                                "text": text})

    def events_since(self, since: int) -> list[dict]:
        with self.lock:
            return [e for e in self.events if e["seq"] > since]

    def emit_ui_event(self, event: dict) -> None:
        self.ui_events.put(event)

    # ------------------------------------------------------------ scheduler
    def request_start(self, queue_id: str, *, force: bool = False) -> bool:
        """Explicit entry point (manual Start button, or an automation
        cycle's auto_start): records that this queue wants to run — which
        persists across a cooldown wait and a restart — and starts it now
        if the shared upload slot is free. Returns True if it actually
        started uploading, False if it's eligible but waiting its turn.
        Raises RuntimeError for things waiting won't fix."""
        rt = self.get(queue_id)
        if not rt.enabled:
            raise RuntimeError("this queue is disabled")
        if not any(i.status == "queued" for i in rt.queue_items):
            raise RuntimeError("queue is empty")
        if force:
            with self.lock:
                limits.set_cooldown(rt._state, None)
                limits.set_quota_cooldown(self.cfg, None)
                config.save_config(self.cfg)
        rt._state["running"] = True
        rt._save_queue_state()
        if not rt.ensure_credentials():
            raise RuntimeError(
                "not signed in to YouTube for this queue's account — it "
                "will resume automatically once it is")
        if not rt.wants_to_run() or self._active_queue_id is not None:
            return False
        rt.start_uploads()
        self._active_queue_id = queue_id
        return True

    def on_worker_done(self, queue_id: str) -> None:
        if self._active_queue_id == queue_id:
            self._active_queue_id = None
        self._advance_rotation(queue_id)

    def _advance_rotation(self, after_id: str | None) -> None:
        """Hands the upload slot to the next eligible queue after `after_id`
        in rotation order (so no single queue hogs priority turn after
        turn). Never preempts an in-flight upload — only called when the
        slot is confirmed free."""
        if self._active_queue_id is not None:
            return
        order = self.list_queues()
        if not order:
            return
        ids = [rt.id for rt in order]
        start = (ids.index(after_id) + 1) % len(ids) if after_id in ids else 0
        for i in range(len(ids)):
            qid = ids[(start + i) % len(ids)]
            rt = self.runtimes[qid]
            if rt.wants_to_run():
                try:
                    rt.start_uploads()
                    self._active_queue_id = qid
                except RuntimeError as exc:
                    rt.log(f"could not start: {exc}")
                return

    # ------------------------------------------------------------- automation
    def _background_loop(self) -> None:
        while True:
            time.sleep(BACKGROUND_TICK_S)
            try:
                self._tick()
            except Exception as exc:
                self.log(f"automation error: {exc}")

    def _tick(self) -> None:
        self._tick_count += 1
        for rt in self.list_queues():
            if rt._state.get("running") and rt.credentials is None \
                    and self._tick_count % CREDENTIAL_RETRY_TICKS == 0:
                rt.ensure_credentials()
            self._auto_tick_one(rt)
        if self._active_queue_id is None:
            self._advance_rotation(None)

    def _auto_tick_one(self, rt: QueueRuntime) -> None:
        if not rt.enabled or not rt.setting("auto_scan"):
            rt.auto_status = "off"
            return
        rt._auto_countdown -= 1
        mins, secs = divmod(max(0, rt._auto_countdown), 60)
        rt.auto_status = f"next scan in {mins}:{secs:02d}"
        if rt._auto_countdown <= 0:
            rt._auto_countdown = max(
                60, int(rt.setting("auto_scan_interval_min", 10)) * 60)
            self.auto_cycle(rt.id)

    def auto_cycle(self, queue_id: str) -> None:
        rt = self.get(queue_id)
        before = set(rt.vods)
        try:
            rt.scan()
        except (FileNotFoundError, RuntimeError) as exc:
            rt.log(f"automation: {exc}")
            return
        new = [k for k in rt.vods if k not in before]
        if new:
            rt.log(f"automation: found {len(new)} new VOD folder(s).")
        busy = {i.key for i in rt.queue_items
                if i.status in ("queued", "uploading", "verifying", "done")}
        candidates = [
            key for key, vod in rt.vods.items()
            if key not in self.registry and key not in busy
            and vod.video_path is not None
            and not (rt.setting("auto_only_finalized", True) and vod.problems)
        ]
        if candidates and rt.setting("auto_queue", True):
            rt.enqueue(candidates)
        if rt.setting("auto_start", True) and \
                any(i.status == "queued" for i in rt.queue_items):
            try:
                self.request_start(rt.id)
            except RuntimeError as exc:
                rt.log(f"automation: {exc}")

    # ------------------------------------------------------------- serialize
    def snapshot(self) -> dict:
        quota_cooldown = limits.get_quota_cooldown(self.cfg)
        return {
            "queues": {rt.id: rt.snapshot() for rt in self.list_queues()},
            "queue_order": [rt.id for rt in self.list_queues()],
            "active_queue_id": self._active_queue_id,
            "quota_cooldown": limits.fmt_local(quota_cooldown) if quota_cooldown else None,
            "quota_cooldown_reason": self.cfg.get("quota_cooldown_reason", ""),
            "accounts": auth.list_accounts(),
            "last_seq": self._seq,
        }
