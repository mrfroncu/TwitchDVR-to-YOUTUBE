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

import json as _json

from app import auth, config, limits, playlists, scanner, ytmanager
from app.scanner import Vod
from app.uploader import QueueItem, UploadWorker, verify_video
from app.version import __version__

from . import deviceauth

VODS_DEFAULT = os.environ.get("VODS_DIR", "/vods")
IS_DESKTOP = os.environ.get("DVR_DESKTOP") == "1"


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
        self.scan_state: dict = {"active": False, "done": 0, "total": 0,
                                 "current": "", "found": None, "error": ""}
        self.lock = threading.RLock()

        self.update_info: dict | None = None
        threading.Thread(target=self._restore_session, daemon=True).start()
        threading.Thread(target=self._background_loop, daemon=True).start()
        if IS_DESKTOP and self.cfg.get("auto_update_check", True):
            threading.Thread(target=self._update_check, daemon=True).start()
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
    def _active_account_id(self) -> str | None:
        accounts = auth.list_accounts()
        ids = [a["id"] for a in accounts]
        active = self.cfg.get("active_account")
        if active in ids:
            return active
        return ids[0] if ids else None

    def _restore_session(self) -> None:
        auth.migrate_legacy_token()
        account_id = self._active_account_id()
        if account_id is None:
            return
        creds = auth.load_account(account_id)
        if creds is None:
            self.log("Saved session can't be reused (expired or new "
                     "permissions needed) — connect YouTube again.")
            return
        self._finish_sign_in(creds)

    def switch_account(self, account_id: str) -> None:
        creds = auth.load_account(account_id)
        if creds is None:
            raise RuntimeError("that account's session expired — connect it again")
        with self.lock:
            self.cfg["active_account"] = account_id
            config.save_config(self.cfg)
            self.playlists = []
            self.playlist_ids = {}
        self._finish_sign_in(creds)

    def _finish_sign_in(self, creds) -> None:
        self.credentials = creds
        self.auth_state = {"status": "signed_in", "detail": ""}
        try:
            service = auth.build_service(creds)
            self.channel = auth.fetch_channel(service)
            name = self.channel["title"] if self.channel else "(no channel)"
            account_id = auth.save_account(creds, self.channel)
            if account_id != "legacy":
                auth.remove_account("legacy")
            with self.lock:
                self.cfg["active_account"] = account_id
                config.save_config(self.cfg)
            self.log(f"Connected to YouTube as channel: {name}")
        except Exception as exc:
            self.channel = None
            self.log("Signed in, but the channel lookup failed: "
                     + auth.describe_api_error(exc))
        self.refresh_playlists()

    # --------------------------------------------------------------- updates
    def _update_check(self) -> None:
        try:
            from app import updater
            info = updater.check_for_update()
            if info:
                self.update_info = info
                self.log(f"New version v{info['version']} is available — see the "
                         "banner at the top.")
        except Exception:
            pass

    def apply_update(self) -> None:
        from app import updater
        if not self.update_info:
            raise RuntimeError("no update available")
        if self.worker and self.worker.is_alive():
            raise RuntimeError("uploads are running — pause them first")
        if not updater.can_self_update():
            raise RuntimeError("this build cannot self-update — download it "
                               f"from {self.update_info['html_url']}")
        self.log(f"Downloading v{self.update_info['version']}…")
        updater.apply_update(self.update_info)
        self.log("Update downloaded — restarting…")
        threading.Timer(1.5, lambda: os._exit(0)).start()

    def start_browser_sign_in(self) -> dict:
        """Desktop-only: classic loopback browser OAuth (works with a normal
        'Desktop app' client, unlike the device flow)."""
        if not IS_DESKTOP:
            raise RuntimeError("browser sign-in only works in the desktop app")
        from google_auth_oauthlib.flow import InstalledAppFlow

        from app.auth import SCOPES, SUCCESS_MESSAGE
        secret_path = (self.cfg.get("client_secret_path") or "").strip()
        client_id = (self.cfg.get("client_id") or "").strip()
        client_secret = (self.cfg.get("client_secret") or "").strip()
        if secret_path and Path(secret_path).exists():
            flow = InstalledAppFlow.from_client_secrets_file(secret_path, SCOPES)
        elif client_id and client_secret:
            flow = InstalledAppFlow.from_client_config({
                "installed": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }}, SCOPES)
        else:
            raise RuntimeError("no OAuth client configured — upload/paste one "
                               "in Settings first")
        self.auth_state = {"status": "pending_browser",
                           "detail": "finish signing in in your browser"}
        self.log("Opening your browser for Google sign-in…")

        def worker():
            try:
                creds = flow.run_local_server(
                    host="127.0.0.1", port=0,
                    prompt="select_account consent",
                    authorization_prompt_message="",
                    success_message=SUCCESS_MESSAGE, open_browser=True)
                self._finish_sign_in(creds)
            except Exception as exc:
                self.auth_state = {"status": "signed_out", "detail": str(exc)[:200]}
                self.log(f"Browser sign-in failed: {exc}")

        threading.Thread(target=worker, daemon=True).start()
        return self.auth_state

    def set_client_secret(self, content: str) -> str:
        """Accept an uploaded client_secret*.json (as text) from the browser."""
        try:
            data = _json.loads(content)
        except ValueError:
            raise RuntimeError("that file is not valid JSON")
        section = data.get("installed") or data.get("web") or {}
        client_id = section.get("client_id", "")
        client_secret = section.get("client_secret", "")
        if not client_id or not client_secret:
            raise RuntimeError("no client_id/client_secret found in the file — "
                               "upload the JSON downloaded from Google Cloud "
                               "Console → Credentials")
        with self.lock:
            self.cfg["client_id"] = client_id
            self.cfg["client_secret"] = client_secret
            config.save_config(self.cfg)
        self.log("OAuth client imported from the uploaded JSON. Note: the web "
                 "version needs a client of type “TVs and Limited Input "
                 "devices” — a Desktop client will be rejected at sign-in.")
        return client_id

    # ------------------------------------------------------------ yt manager
    def _service(self):
        if self.credentials is None:
            raise RuntimeError("not signed in to YouTube")
        return auth.build_service(self.credentials)

    def yt_list(self) -> list[dict]:
        return ytmanager.list_channel_videos(self._service())

    def yt_video(self, video_id: str) -> dict:
        return ytmanager.get_video(self._service(), video_id)

    def yt_update(self, video_id: str, patch: dict) -> None:
        current = ytmanager.get_video(self._service(), video_id)
        merged = {**current, **{k: v for k, v in patch.items() if v is not None}}
        tags = merged["tags"]
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        ytmanager.update_video(
            self._service(), video_id,
            title=scanner.sanitize_title(str(merged["title"])),
            description=str(merged["description"])[:4990],
            tags=tags,
            category_id=str(merged["category_id"]),
            privacy=str(merged["privacy"]))
        self.log(f"Saved changes to video {video_id} on YouTube.")

    def yt_video_playlists(self, video_id: str) -> list[dict]:
        with self.lock:
            channel_playlists = list(self.playlists)
        return ytmanager.video_playlists(self._service(), channel_playlists,
                                         video_id)

    def yt_playlist_add(self, video_id: str, playlist_id: str) -> None:
        playlists.add_to_playlist(self._service(), playlist_id, video_id)
        self.log(f"Added video {video_id} to a playlist.")

    def yt_playlist_remove(self, item_id: str) -> None:
        ytmanager.remove_from_playlist(self._service(), item_id)
        self.log("Removed the video from the playlist.")

    def yt_bulk(self, action: str, ids: list[str], value: str = "") -> dict:
        service = self._service()
        ok = 0
        errors: list[str] = []
        for vid in ids:
            try:
                if action == "privacy":
                    ytmanager.set_privacy(service, vid, value)
                elif action == "playlist":
                    playlists.add_to_playlist(service, value, vid)
                elif action == "delete":
                    ytmanager.delete_video(service, vid)
                else:
                    raise RuntimeError(f"unknown action {action}")
                ok += 1
            except Exception as exc:
                errors.append(f"{vid}: {auth.describe_api_error(exc)[:120]}")
        self.log(f"YT manager: {action} applied to {ok}/{len(ids)} video(s).")
        for err in errors[:3]:
            self.log(err)
        return {"ok": ok, "errors": errors}

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
        account_id = self._active_account_id()
        if account_id:
            auth.remove_account(account_id)
        with self.lock:
            self.cfg["active_account"] = ""
            config.save_config(self.cfg)
        self.credentials = None
        self.channel = None
        self._device_flow = None
        self.auth_state = {"status": "signed_out", "detail": ""}
        self.log("Removed the active account.")
        remaining = self._active_account_id()
        if remaining:
            try:
                self.switch_account(remaining)
            except RuntimeError:
                pass

    # ------------------------------------------------------------------- scan
    def scan(self, folder: str | None = None) -> int:
        """Blocking scan; live progress is published via self.scan_state."""
        folder = (folder or self.cfg.get("vod_folder") or VODS_DEFAULT).strip()
        root = Path(folder)
        if not root.is_dir():
            self.scan_state.update(active=False, found=None,
                                   error=f"folder does not exist: {folder}")
            raise FileNotFoundError(f"folder does not exist: {folder}")
        if self.scan_state.get("active"):
            raise RuntimeError("a scan is already running")
        self.scan_state.update(active=True, done=0, total=0, current="",
                               found=None, error="")
        with self.lock:
            self.cfg["vod_folder"] = folder
            config.save_config(self.cfg)
        try:
            vods = scanner.scan_folder(
                root, progress=lambda done, total, name: self.scan_state.update(
                    done=done, total=total, current=name))
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
        self.scan_state.update(active=False, found=len(vods))
        self.log(f"Scanned {folder}: found {len(vods)} VOD folder(s)."
                 if vods else f"Scanned {folder}: no VOD folders found.")
        return len(vods)

    def scan_async(self, folder: str | None = None) -> None:
        if self.scan_state.get("active"):
            raise RuntimeError("a scan is already running")

        def worker():
            try:
                self.scan(folder)
            except (FileNotFoundError, RuntimeError) as exc:
                self.log(f"Scan failed: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def _generate_meta(self, vod: Vod) -> dict:
        tags = scanner.build_tags(vod)
        seen = {t.lower() for t in tags}
        for tag in (t.strip() for t in self.cfg.get("extra_tags", "").split(",")):
            if tag and tag.lower() not in seen:
                tags.append(tag)
                seen.add(tag.lower())
        return {
            "title": scanner.build_title(vod, self.cfg["title_template"]),
            "description": scanner.build_description(
                vod, self.cfg.get("description_template") or None),
            "tags": ", ".join(tags),
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

    def start_uploads(self, force: bool = False) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not any(i.status == "queued" for i in self.queue_items):
            raise RuntimeError("queue is empty")
        if self.credentials is None:
            raise RuntimeError("not signed in to YouTube")
        cooldown = limits.get_cooldown(self.cfg)
        if cooldown is not None:
            if not force:
                raise RuntimeError(
                    f"upload cooldown until {limits.fmt_local(cooldown)} "
                    f"({self.cfg.get('cooldown_reason', 'limit')}) — uploads "
                    "resume automatically, or force-start to override")
            limits.set_cooldown(self.cfg, None)
            config.save_config(self.cfg)
        import queue as _q
        self.worker_events = _q.Queue()
        self.worker = UploadWorker(
            self.credentials, self.queue_items, self.worker_events,
            daily_limit=int(self.cfg.get("daily_upload_limit", 0) or 0),
            count_recent=lambda: limits.count_recent(self.registry),
            speed_limit_bps=float(self.cfg.get("upload_speed_limit", 0) or 0) * 1e6,
            verify=bool(self.cfg.get("verify_uploads", True)))
        self.worker.start()
        threading.Thread(target=self._pump_worker,
                         args=(self.worker_events,), daemon=True).start()
        self.log("Upload queue started.")

    def retry_failed(self) -> int:
        count = 0
        with self.lock:
            for item in self.queue_items:
                if item.status in ("error", "cancelled"):
                    item.status = "queued"
                    item.detail = ""
                    item.progress = 0.0
                    count += 1
        self.log(f"Re-queued {count} failed upload(s)." if count
                 else "No failed uploads in the queue.")
        return count

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
                if reason == "quota":
                    until = limits.quota_cooldown(ev.get("detail", ""),
                                                  self.cfg.get("cooldown_hours"))
                    with self.lock:
                        limits.set_cooldown(self.cfg, until,
                                            ev.get("detail", "limit"))
                        config.save_config(self.cfg)
                    self.log(f"Cooldown until {limits.fmt_local(until)} — "
                             "uploads resume automatically.")
                elif reason == "daily_limit":
                    until = limits.next_slot(
                        self.registry,
                        int(self.cfg.get("daily_upload_limit", 0) or 0))
                    with self.lock:
                        limits.set_cooldown(self.cfg, until, "daily upload limit")
                        config.save_config(self.cfg)
                    self.log(f"Daily limit reached — next upload at "
                             f"{limits.fmt_local(until)} (automatic).")
                else:
                    self.log({"finished": "Queue finished.",
                              "paused": "Queue paused."}.get(reason, "Idle."))

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
                self._cooldown_tick()
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

    def _cooldown_tick(self) -> None:
        """Auto-resume the queue once a stored cooldown expires."""
        if not self.cfg.get("cooldown_until"):
            return
        if limits.get_cooldown(self.cfg) is not None:
            return
        with self.lock:
            limits.set_cooldown(self.cfg, None)
            config.save_config(self.cfg)
        if any(i.status == "queued" for i in self.queue_items) and \
                not (self.worker and self.worker.is_alive()) and \
                self.credentials is not None:
            self.log("Upload cooldown finished — resuming the queue.")
            try:
                self.start_uploads()
            except RuntimeError as exc:
                self.log(f"Auto-resume failed: {exc}")

    def auto_cycle(self) -> None:
        before = set(self.vods)
        try:
            self.scan()
        except (FileNotFoundError, RuntimeError) as exc:
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
            cooldown = limits.get_cooldown(self.cfg)
            if cooldown is not None:
                self.log("Automation: waiting for the upload cooldown "
                         f"(until {limits.fmt_local(cooldown)}).")
            elif self.credentials is None:
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
                "video_id": i.video_id,
            } for i in self.queue_items]
            cooldown = limits.get_cooldown(self.cfg)
            return {
                "version": __version__,
                "desktop": IS_DESKTOP,
                "update": self.update_info,
                "scan": dict(self.scan_state),
                "cooldown": limits.fmt_local(cooldown) if cooldown else None,
                "cooldown_reason": self.cfg.get("cooldown_reason", ""),
                "uploads_last_24h": limits.count_recent(self.registry),
                "auth": {**self.auth_state,
                         "channel": self.channel["title"] if self.channel else None},
                "accounts": auth.list_accounts(),
                "active_account": self.cfg.get("active_account", ""),
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
