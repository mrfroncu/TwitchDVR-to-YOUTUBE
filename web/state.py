"""Headless controller for the web version.

Owns the same state the desktop GUI does (VOD list, editable metadata,
upload queue, worker, automation, playlists) but exposes it as plain data
plus an incrementing event log that the browser polls.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from app import auth, config, playlists, scanner
from app.scanner import Vod
from app.uploader import QueueItem, UploadWorker, verify_video
from app.version import __version__

from . import deviceauth

VODS_DEFAULT = os.environ.get("VODS_DIR", "/vods")


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


class Controller:
    def __init__(self):
        self.cfg = config.load_config()
        if not self.cfg.get("vod_folder"):
            self.cfg["vod_folder"] = VODS_DEFAULT
        self.registry = config.load_registry()
        self.vods: dict[str, Vod] = {}
        self.metas: dict[str, dict] = {}
        self.queue_items: list[QueueItem] = []
        self.worker: UploadWorker | None = None
        self.worker_events = None   # queue.Queue owned by the worker
        self.credentials = None
        self.channel: dict | None = None
        self.playlists: list[dict] = []
        self.playlist_ids: dict[str, str] = {}
        self.auth_state = {"status": "signed_out", "detail": ""}
        self._device_flow: dict | None = None
        self.events: deque = deque(maxlen=800)
        self._seq = 0
        self._auto_countdown = int(self.cfg.get("auto_scan_interval_min", 10)) * 60
        self.auto_status = "off"
        self.lock = threading.RLock()

        threading.Thread(target=self._restore_session, daemon=True).start()
        threading.Thread(target=self._background_loop, daemon=True).start()
        if Path(self.cfg["vod_folder"]).is_dir():
            try:
                self.scan()
            except Exception as exc:
                self.log(f"Initial scan failed: {exc}")

    # ------------------------------------------------------------------ events
    def log(self, text: str) -> None:
        with self.lock:
            self._seq += 1
            self.events.append({
                "seq": self._seq,
                "time": datetime.now().strftime("%H:%M:%S"),
                "text": text,
            })

    def events_since(self, since: int) -> list[dict]:
        with self.lock:
            return [e for e in self.events if e["seq"] > since]

    # ------------------------------------------------------------------- auth
    def _restore_session(self) -> None:
        creds = auth.load_credentials()
        if creds is None:
            if config.TOKEN_PATH.exists():
                self.log("Saved session can't be reused (expired or new "
                         "permissions needed) — connect YouTube again.")
            return
        self._finish_sign_in(creds)

    def _finish_sign_in(self, creds) -> None:
        self.credentials = creds
        self.auth_state = {"status": "signed_in", "detail": ""}
        try:
            service = auth.build_service(creds)
            self.channel = auth.fetch_channel(service)
            name = self.channel["title"] if self.channel else "(no channel)"
            self.log(f"Connected to YouTube as channel: {name}")
        except Exception as exc:
            self.channel = None
            self.log("Signed in, but the channel lookup failed: "
                     + auth.describe_api_error(exc))
        self.refresh_playlists()

    def start_sign_in(self) -> dict:
        client_id, client_secret = deviceauth.read_client_from_config(self.cfg)
        flow = deviceauth.start(client_id)
        self._device_flow = flow
        self.auth_state = {
            "status": "pending",
            "user_code": flow["user_code"],
            "verification_url": flow.get("verification_url",
                                         "https://www.google.com/device"),
            "detail": "",
        }
        self.log(f"Sign-in started — enter code {flow['user_code']} at "
                 f"{self.auth_state['verification_url']}")

        def poller():
            deadline = time.monotonic() + int(flow.get("expires_in", 1800))
            interval = int(flow.get("interval", 5))
            while time.monotonic() < deadline:
                if self._device_flow is not flow:
                    return      # a newer flow replaced this one
                try:
                    creds = deviceauth.poll_once(client_id, client_secret,
                                                 flow["device_code"])
                except Exception as exc:
                    self.auth_state = {"status": "signed_out", "detail": str(exc)}
                    self.log(f"Sign-in failed: {exc}")
                    return
                if creds is not None:
                    self._finish_sign_in(creds)
                    return
                time.sleep(interval)
            self.auth_state = {"status": "signed_out",
                               "detail": "sign-in code expired"}

        threading.Thread(target=poller, daemon=True).start()
        return self.auth_state

    def sign_out(self) -> None:
        auth.sign_out()
        self.credentials = None
        self.channel = None
        self._device_flow = None
        self.auth_state = {"status": "signed_out", "detail": ""}
        self.log("Signed out.")

    # ------------------------------------------------------------------- scan
    def scan(self, folder: str | None = None) -> int:
        folder = (folder or self.cfg.get("vod_folder") or VODS_DEFAULT).strip()
        root = Path(folder)
        if not root.is_dir():
            raise FileNotFoundError(f"folder does not exist: {folder}")
        with self.lock:
            self.cfg["vod_folder"] = folder
            config.save_config(self.cfg)
            vods = scanner.scan_folder(root)
            self.vods = {v.key: v for v in vods}
            for key in list(self.metas):
                if key not in self.vods:
                    del self.metas[key]
            for vod in vods:
                if vod.key not in self.metas:
                    self.metas[vod.key] = self._generate_meta(vod)
        self.log(f"Scanned {folder}: found {len(vods)} VOD folder(s).")
        return len(vods)

    def _generate_meta(self, vod: Vod) -> dict:
        return {
            "title": scanner.build_title(vod, self.cfg["title_template"]),
            "description": scanner.build_description(vod),
            "tags": ", ".join(scanner.build_tags(vod)),
            "privacy": self.cfg["privacy"],
            "playlist_choice": "(default)",
        }

    def vod_status(self, vod: Vod) -> str:
        entry = self.registry.get(vod.key)
        if entry:
            if entry.get("failed"):
                return "failed on YouTube"
            if entry.get("local_deleted"):
                return "uploaded ✓ · cleaned"
            if entry.get("verified"):
                return "uploaded ✓ verified"
            return "uploaded (unverified)"
        for item in self.queue_items:
            if item.key == vod.key and item.status in ("queued", "uploading",
                                                       "verifying"):
                return item.status
        if vod.problems:
            return ", ".join(vod.problems)
        return "ready"

    # -------------------------------------------------------------- playlists
    def refresh_playlists(self) -> None:
        if self.credentials is None:
            return

        def worker():
            try:
                service = auth.build_service(self.credentials)
                items = playlists.list_playlists(service)
                with self.lock:
                    self.playlists = items
                    self.playlist_ids = {p["title"]: p["id"] for p in items}
                self.log(f"Loaded {len(items)} playlist(s) from the channel.")
            except Exception as exc:
                self.log("Could not load playlists: "
                         + auth.describe_api_error(exc))

        threading.Thread(target=worker, daemon=True).start()

    def create_playlist(self, title: str, privacy: str) -> None:
        if self.credentials is None:
            raise RuntimeError("not signed in")

        def worker():
            try:
                service = auth.build_service(self.credentials)
                playlists.create_playlist(service, title, privacy)
                self.log(f"Created playlist '{title}' ({privacy})")
                self.refresh_playlists()
            except Exception as exc:
                self.log("Could not create playlist: "
                         + auth.describe_api_error(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _render_playlist_name(self, vod: Vod | None) -> str:
        if vod is None:
            return ""
        template = self.cfg.get("playlist_template") or "{streamer} VODs"
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

    def _resolve_playlist_spec(self, key: str) -> dict | None:
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

    # ------------------------------------------------------------------ queue
    def enqueue(self, keys: list[str]) -> tuple[int, int]:
        added = skipped = 0
        with self.lock:
            busy = {i.key for i in self.queue_items
                    if i.status in ("queued", "uploading", "verifying", "done")}
            for key in keys:
                vod = self.vods.get(key)
                if vod is None:
                    continue
                entry = self.registry.get(key)
                if (entry and not entry.get("failed")) or key in busy \
                        or vod.video_path is None:
                    skipped += 1
                    continue
                meta = self.metas[key]
                self.queue_items.append(QueueItem(
                    vod=vod,
                    title=scanner.sanitize_title(meta["title"]),
                    description=meta["description"],
                    tags=[t.strip() for t in meta["tags"].split(",") if t.strip()],
                    privacy=meta["privacy"],
                    category_id=str(self.cfg.get("category_id", "20")),
                    recording_date=scanner.recording_date(vod),
                    notify_subscribers=bool(self.cfg.get("notify_subscribers", False)),
                    made_for_kids=bool(self.cfg.get("made_for_kids", False)),
                    playlist=self._resolve_playlist_spec(key),
                ))
                added += 1
        if added or skipped:
            self.log(f"Queued {added} video(s)"
                     + (f", skipped {skipped}." if skipped else "."))
        return added, skipped

    def remove_from_queue(self, keys: list[str]) -> None:
        with self.lock:
            self.queue_items = [
                i for i in self.queue_items
                if i.key not in keys or i.status in ("uploading", "verifying")]

    def move_in_queue(self, key: str, delta: int) -> None:
        with self.lock:
            idx = next((i for i, item in enumerate(self.queue_items)
                        if item.key == key), None)
            if idx is None:
                return
            new = max(0, min(len(self.queue_items) - 1, idx + delta))
            item = self.queue_items.pop(idx)
            self.queue_items.insert(new, item)

    def clear_finished(self) -> None:
        with self.lock:
            self.queue_items = [i for i in self.queue_items
                                if i.status not in ("done", "cancelled")]

    def start_uploads(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not any(i.status == "queued" for i in self.queue_items):
            raise RuntimeError("queue is empty")
        if self.credentials is None:
            raise RuntimeError("not signed in to YouTube")
        import queue as _q
        self.worker_events = _q.Queue()
        self.worker = UploadWorker(self.credentials, self.queue_items,
                                   self.worker_events)
        self.worker.start()
        threading.Thread(target=self._pump_worker,
                         args=(self.worker_events,), daemon=True).start()
        self.log("Upload queue started.")

    def pause_uploads(self) -> None:
        if self.worker:
            self.worker.pause_requested.set()
            self.log("Will pause after the current upload.")

    def cancel_current(self) -> None:
        if self.worker:
            self.worker.cancel_current.set()
            self.log("Cancelling current upload…")

    def _pump_worker(self, events) -> None:
        import queue as _q
        while True:
            try:
                ev = events.get(timeout=1.0)
            except _q.Empty:
                if not (self.worker and self.worker.is_alive()):
                    return
                continue
            etype = ev.get("type")
            if etype == "log":
                self.log(ev["text"])
            elif etype == "item_status" and ev["status"] == "done":
                verified = bool(ev.get("verified"))
                with self.lock:
                    self.registry[ev["key"]] = {
                        "video_id": ev.get("video_id"),
                        "title": next((i.title for i in self.queue_items
                                       if i.key == ev["key"]), ""),
                        "uploaded_at": datetime.now(timezone.utc).isoformat(),
                        "verified": verified,
                    }
                    config.save_registry(self.registry)
                mode = self.cfg.get("after_upload", "keep")
                if verified and mode != "keep":
                    self._dispose_vod(ev["key"], mode)
            elif etype == "progress":
                with self.lock:
                    for item in self.queue_items:
                        if item.key == ev["key"]:
                            item.progress = ev["pct"]
                            if ev.get("speed_bps"):
                                item.detail = (f"{ev['speed_bps'] / 1e6:.1f} MB/s, "
                                               f"ETA {int(ev['eta_s'])}s")
            elif etype == "worker_done":
                reason = ev.get("reason", "")
                self.log({"finished": "Queue finished.",
                          "paused": "Queue paused.",
                          "quota": "Stopped: daily API quota exhausted "
                                   "(resets midnight Pacific)."}.get(reason, "Idle."))

    # ---------------------------------------------------------- housekeeping
    def _dispose_vod(self, key: str, mode: str) -> bool:
        """Server mode has no Recycle Bin — deletion is permanent."""
        vod = self.vods.get(key)
        if vod is None:
            return False
        target = vod.folder if mode == "trash_folder" else vod.video_path
        if target is None or not target.exists():
            return False
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as exc:
            self.log(f"Could not delete {target}: {exc}")
            return False
        with self.lock:
            entry = self.registry.setdefault(key, {})
            entry["local_deleted"] = True
            config.save_registry(self.registry)
        self.log(f"Deleted local files: {target}")
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
            self.log(f"Reset metadata for {len(keys)} video(s).")
            return {"count": len(keys)}
        if action == "privacy":
            with self.lock:
                for key in keys:
                    self.metas[key]["privacy"] = value
            self.log(f"Set privacy '{value}' on {len(keys)} video(s).")
            return {"count": len(keys)}
        if action == "playlist":
            with self.lock:
                for key in keys:
                    self.metas[key]["playlist_choice"] = value or "(default)"
            self.log(f"Set playlist '{value}' on {len(keys)} video(s).")
            return {"count": len(keys)}
        if action == "reset_state":
            with self.lock:
                cleared = [k for k in keys if self.registry.pop(k, None) is not None]
                config.save_registry(self.registry)
            self.log(f"Reset upload state of {len(cleared)} video(s).")
            return {"count": len(cleared)}
        if action == "verify":
            targets = [k for k in keys if self.registry.get(k, {}).get("video_id")]
            if not targets:
                raise RuntimeError("none of these were uploaded yet")
            if self.credentials is None:
                raise RuntimeError("not signed in")
            threading.Thread(target=self._verify_worker, args=(targets,),
                             daemon=True).start()
            return {"count": len(targets)}
        if action == "delete_local":
            mode = self.cfg.get("after_upload", "keep")
            mode = "trash_video" if mode == "keep" else mode
            count = 0
            for key in keys:
                entry = self.registry.get(key)
                if entry and entry.get("verified") and not entry.get("local_deleted"):
                    if self._dispose_vod(key, mode):
                        count += 1
            self.scan()
            return {"count": count}
        raise RuntimeError(f"unknown bulk action: {action}")

    def _verify_worker(self, keys: list[str]) -> None:
        try:
            service = auth.build_service(self.credentials)
        except Exception as exc:
            self.log(f"Verify failed: {exc}")
            return
        for key in keys:
            video_id = self.registry[key]["video_id"]
            try:
                ok, detail = verify_video(service, video_id)
            except Exception as exc:
                ok, detail = None, str(exc)[:200]
            with self.lock:
                entry = self.registry.get(key)
                if entry is not None and ok is not None:
                    entry["verified"] = bool(ok)
                    entry["failed"] = not ok
                    entry["verify_detail"] = detail
                    config.save_registry(self.registry)
            state = {True: "OK", False: "MISSING/FAILED",
                     None: "check unavailable"}[ok]
            self.log(f"Verify {key} (youtu.be/{video_id}): {state} — {detail}")

    # -------------------------------------------------------------- automation
    def _background_loop(self) -> None:
        while True:
            time.sleep(1)
            try:
                if not self.cfg.get("auto_scan"):
                    self.auto_status = "off"
                    continue
                self._auto_countdown -= 1
                mins, secs = divmod(max(0, self._auto_countdown), 60)
                self.auto_status = f"next scan in {mins}:{secs:02d}"
                if self._auto_countdown <= 0:
                    self._auto_countdown = max(
                        60, int(self.cfg.get("auto_scan_interval_min", 10)) * 60)
                    self.auto_cycle()
            except Exception as exc:
                self.log(f"Automation error: {exc}")

    def auto_cycle(self) -> None:
        before = set(self.vods)
        try:
            self.scan()
        except FileNotFoundError as exc:
            self.log(f"Automation: {exc}")
            return
        new = [k for k in self.vods if k not in before]
        if new:
            self.log(f"Automation: found {len(new)} new VOD folder(s).")
        busy = {i.key for i in self.queue_items
                if i.status in ("queued", "uploading", "verifying", "done")}
        candidates = []
        for key, vod in self.vods.items():
            if key in self.registry or key in busy or vod.video_path is None:
                continue
            if self.cfg.get("auto_only_finalized", True) and vod.problems:
                continue
            candidates.append(key)
        if candidates and self.cfg.get("auto_queue", True):
            self.enqueue(candidates)
        if self.cfg.get("auto_start", True) and \
                any(i.status == "queued" for i in self.queue_items) and \
                not (self.worker and self.worker.is_alive()):
            if self.credentials is None:
                self.log("Automation: queue has items but not signed in.")
            else:
                self.log("Automation: starting uploads.")
                self.start_uploads()

    # ------------------------------------------------------------- serialize
    def snapshot(self) -> dict:
        with self.lock:
            vods = []
            for vod in self.vods.values():
                meta = self.metas.get(vod.key, {})
                vods.append({
                    "key": vod.key,
                    "date": vod.date_str,
                    "streamer": vod.streamer_name or vod.streamer_login,
                    "stream_title": vod.stream_title,
                    "duration": vod.duration,
                    "size": vod.size_bytes,
                    "chapters": len(vod.chapters),
                    "problems": vod.problems,
                    "status": self.vod_status(vod),
                    "meta": meta,
                })
            queue = [{
                "key": i.key, "title": i.title, "size": i.vod.size_bytes,
                "privacy": i.privacy, "status": i.status,
                "detail": i.detail, "progress": round(i.progress, 1),
            } for i in self.queue_items]
            return {
                "version": __version__,
                "auth": {**self.auth_state,
                         "channel": self.channel["title"] if self.channel else None},
                "cfg": {k: v for k, v in self.cfg.items() if k != "client_secret"},
                "has_client_secret": bool(self.cfg.get("client_secret")),
                "vods": vods,
                "queue": queue,
                "uploading": bool(self.worker and self.worker.is_alive()),
                "playlists": self.playlists,
                "auto_status": self.auto_status,
                "last_seq": self._seq,
            }

    def update_settings(self, patch: dict) -> None:
        allowed = set(config.DEFAULTS) | {"vod_folder"}
        with self.lock:
            for key, value in patch.items():
                if key in allowed:
                    self.cfg[key] = value
            config.save_config(self.cfg)
        self.log("Settings saved.")

    def update_meta(self, key: str, patch: dict) -> None:
        with self.lock:
            meta = self.metas.get(key)
            if meta is None:
                raise KeyError(key)
            for field in ("title", "description", "tags", "privacy",
                          "playlist_choice"):
                if field in patch:
                    meta[field] = patch[field]
            meta["title"] = scanner.sanitize_title(meta["title"])
