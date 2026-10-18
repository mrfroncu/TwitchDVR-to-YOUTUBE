"""Headless controller for the web version.

Wraps one shared `app.queue_runtime.QueueManager` (scan/queue/upload/
cooldown logic for every configured queue — see that module for the actual
model) with the concerns that aren't queue-scoped: the YouTube account pool
and its sign-in flows, update checks, the "My YouTube" manager (browsing an
arbitrary signed-in channel's existing videos, independent of any one
queue), and LiveStreamDVR connectivity testing.
"""
from __future__ import annotations

import json as _json
import os
import threading
import time
from pathlib import Path

from app import auth, config, lsdvr, playlists, scanner, ytmanager
from app.queue_runtime import QueueManager, QueueRuntime
from app.version import __version__

from . import deviceauth

VODS_DEFAULT = os.environ.get("VODS_DIR", "/vods")
IS_DESKTOP = os.environ.get("DVR_DESKTOP") == "1"


class Controller:
    def __init__(self):
        self.manager = QueueManager()
        self.cfg = self.manager.cfg
        self.auth_state = {"status": "signed_out", "detail": ""}
        self._device_flow: dict | None = None
        self.update_info: dict | None = None
        if auth.list_accounts():
            self.auth_state = {"status": "signed_in", "detail": ""}
        if IS_DESKTOP and self.cfg.get("auto_update_check", True):
            threading.Thread(target=self._update_check, daemon=True).start()

    # ------------------------------------------------------------------ log
    def log(self, text: str) -> None:
        self.manager.log(text)

    def events_since(self, since: int) -> list[dict]:
        return self.manager.events_since(since)

    # --------------------------------------------------------------- queues
    def get_queue(self, queue_id: str) -> QueueRuntime:
        return self.manager.get(queue_id)   # raises KeyError if unknown

    def create_queue(self, name: str, vod_folder: str, account_id: str = "") -> dict:
        rt = self.manager.create_queue(name, vod_folder.strip(), account_id)
        return rt.snapshot()

    def update_queue(self, queue_id: str, patch: dict) -> dict:
        rt = self.manager.update_queue(queue_id, patch)
        return rt.snapshot()

    def remove_queue(self, queue_id: str) -> None:
        self.manager.remove_queue(queue_id)

    def reorder_queues(self, ordered_ids: list[str]) -> None:
        self.manager.reorder_queues(ordered_ids)

    # ------------------------------------------------------- per-queue ops
    def scan(self, queue_id: str, folder: str | None = None) -> int:
        return self.get_queue(queue_id).scan(folder)

    def scan_async(self, queue_id: str, folder: str | None = None) -> None:
        self.get_queue(queue_id).scan_async(folder)

    def update_meta(self, queue_id: str, key: str, patch: dict) -> None:
        self.get_queue(queue_id).update_meta(key, patch)

    def bulk(self, queue_id: str, action: str, keys: list[str], value: str = "") -> dict:
        return self.get_queue(queue_id).bulk(action, keys, value)

    def start_uploads(self, queue_id: str, force: bool = False) -> bool:
        return self.manager.request_start(queue_id, force=force)

    def retry_failed(self, queue_id: str) -> int:
        return self.get_queue(queue_id).retry_failed()

    def pause_uploads(self, queue_id: str) -> None:
        self.get_queue(queue_id).pause_uploads()

    def cancel_current(self, queue_id: str) -> None:
        self.get_queue(queue_id).cancel_current()

    def remove_from_queue(self, queue_id: str, keys: list[str]) -> None:
        self.get_queue(queue_id).remove_from_queue(keys)

    def move_in_queue(self, queue_id: str, key: str, delta: int) -> None:
        self.get_queue(queue_id).move_in_queue(key, delta)

    def clear_finished(self, queue_id: str) -> None:
        self.get_queue(queue_id).clear_finished()

    def history(self, queue_id: str) -> list[dict]:
        return self.get_queue(queue_id).history()

    def automation_run(self, queue_id: str) -> None:
        self.manager.auto_cycle(queue_id)

    def get_vod(self, queue_id: str, key: str):
        return self.get_queue(queue_id).vods.get(key)

    # ------------------------------------------------------------------- auth
    def _finish_sign_in(self, creds) -> str:
        """Persists `creds` under its channel id. Does NOT assign it to any
        queue — the user picks which queue(s) use it from the Queues editor.
        Returns the new account id."""
        channel = None
        try:
            channel = auth.fetch_channel(auth.build_service(creds))
        except Exception as exc:
            self.log("Signed in, but the channel lookup failed: "
                     + auth.describe_api_error(exc))
        name = channel["title"] if channel else "(no channel on this account)"
        account_id = auth.save_account(creds, channel)
        if account_id != "legacy":
            auth.remove_account("legacy")
        self.auth_state = {"status": "signed_in", "detail": ""}
        self.log(f"Connected to YouTube as channel: {name} — assign it to a "
                 "queue from the Queues tab.")
        return account_id

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
        with self.manager.lock:
            self.cfg["client_id"] = client_id
            self.cfg["client_secret"] = client_secret
            config.save_config(self.cfg)
        self.log("OAuth client imported from the uploaded JSON. Note: the web "
                 "version needs a client of type “TVs and Limited Input "
                 "devices” — a Desktop client will be rejected at sign-in.")
        return client_id

    def remove_account(self, account_id: str) -> None:
        auth.remove_account(account_id)
        for rt in self.manager.list_queues():
            if rt.account_id == account_id:
                rt.forget_credentials()
        self.log(f"Removed the saved sign-in for account {account_id}.")
        if not auth.list_accounts():
            self.auth_state = {"status": "signed_out", "detail": ""}

    # ------------------------------------------------------------ yt manager
    def _service_for(self, account_id: str):
        creds = auth.load_account(account_id)
        if creds is None:
            raise RuntimeError("that account's session expired — add it again")
        return auth.build_service(creds)

    def yt_list(self, account_id: str) -> list[dict]:
        return ytmanager.list_channel_videos(self._service_for(account_id))

    def yt_video(self, account_id: str, video_id: str) -> dict:
        return ytmanager.get_video(self._service_for(account_id), video_id)

    def yt_update(self, account_id: str, video_id: str, patch: dict) -> None:
        service = self._service_for(account_id)
        current = ytmanager.get_video(service, video_id)
        merged = {**current, **{k: v for k, v in patch.items() if v is not None}}
        tags = merged["tags"]
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        ytmanager.update_video(
            service, video_id,
            title=scanner.sanitize_title(str(merged["title"])),
            description=str(merged["description"])[:4990],
            tags=tags,
            category_id=str(merged["category_id"]),
            privacy=str(merged["privacy"]))
        self.log(f"Saved changes to video {video_id} on YouTube.")

    def yt_video_playlists(self, account_id: str, video_id: str) -> list[dict]:
        service = self._service_for(account_id)
        channel_playlists = playlists.list_playlists(service)
        return ytmanager.video_playlists(service, channel_playlists, video_id)

    def yt_playlist_add(self, account_id: str, video_id: str, playlist_id: str) -> None:
        playlists.add_to_playlist(self._service_for(account_id), playlist_id, video_id)
        self.log(f"Added video {video_id} to a playlist.")

    def playlists_list(self, account_id: str) -> list[dict]:
        return playlists.list_playlists(self._service_for(account_id))

    def playlists_create(self, account_id: str, title: str, privacy: str) -> None:
        playlists.create_playlist(self._service_for(account_id), title, privacy)
        self.log(f"Created playlist '{title}' ({privacy}).")

    def yt_playlist_remove(self, account_id: str, item_id: str) -> None:
        ytmanager.remove_from_playlist(self._service_for(account_id), item_id)
        self.log("Removed the video from the playlist.")

    def yt_bulk(self, account_id: str, action: str, ids: list[str],
                value: str = "") -> dict:
        service = self._service_for(account_id)
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

    def check_updates_now(self) -> dict | None:
        from app import updater
        try:
            self.update_info = updater.check_for_update()
        except Exception as exc:
            raise RuntimeError(f"update check failed: {exc}")
        if self.update_info:
            self.log(f"New version v{self.update_info['version']} is available.")
        return self.update_info

    def apply_update(self) -> None:
        from app import updater
        if not self.update_info:
            raise RuntimeError("no update available")
        if any(rt.worker and rt.worker.is_alive() for rt in self.manager.list_queues()):
            raise RuntimeError("uploads are running — pause them first")
        if not updater.can_self_update():
            raise RuntimeError("this build cannot self-update — download it "
                               f"from {self.update_info['html_url']}")
        self.log(f"Downloading v{self.update_info['version']}…")
        updater.apply_update(self.update_info)
        self.log("Update downloaded — restarting…")
        threading.Timer(1.5, lambda: os._exit(0)).start()

    # -------------------------------------------------------- lsdvr/settings
    def lsdvr_test(self, url: str = "", password: str = "") -> str:
        """Settings → Test connection. Takes what is typed in the form, so it
        works before the fields are saved; falls back to the saved values."""
        client = lsdvr.Client(url or self.cfg.get("lsdvr_url", ""),
                              password or self.cfg.get("lsdvr_password", ""))
        return client.check()

    def update_settings(self, patch: dict) -> None:
        """App-wide settings only (theme/OAuth client/LiveStreamDVR/update
        check/queue_ui_layout) — per-queue settings go through update_queue."""
        with self.manager.lock:
            self.cfg.update(patch)
            config.save_config(self.cfg)

    # ------------------------------------------------------------- serialize
    def snapshot(self) -> dict:
        m = self.manager.snapshot()
        return {
            "version": __version__,
            "desktop": IS_DESKTOP,
            "update": self.update_info,
            "auth": self.auth_state,
            "accounts": m["accounts"],
            "cfg": {k: v for k, v in self.cfg.items()
                    if k not in ("client_secret", "lsdvr_password")},
            "has_client_secret": bool(self.cfg.get("client_secret")),
            "has_lsdvr_password": bool(self.cfg.get("lsdvr_password")),
            "vods_default": VODS_DEFAULT,
            "queues": m["queues"],
            "queue_order": m["queue_order"],
            "active_queue_id": m["active_queue_id"],
            "quota_cooldown": m["quota_cooldown"],
            "quota_cooldown_reason": m["quota_cooldown_reason"],
            "last_seq": m["last_seq"],
        }
