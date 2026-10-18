"""FastAPI app: JSON API + static frontend for the browser version.

Set the WEB_PASSWORD environment variable (e.g. via an .env file next to
docker-compose.yml) to require signing in. The login form lives in the web
UI itself; a successful login sets an HttpOnly session cookie (valid until
the container restarts).

Most routes are scoped to one queue (/api/queues/{queue_id}/...) — see
app/queue_runtime.py for what a "queue" is. A handful of routes are
app-wide (settings, the YouTube account pool, update checks) or account-
scoped rather than queue-scoped (the "My YouTube" manager, which browses an
arbitrary signed-in channel's existing videos).
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.version import __version__

from .state import Controller

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "dvr2yt_session"
SESSION_TOKEN = secrets.token_hex(32)


class ScanBody(BaseModel):
    folder: str | None = None


class KeysBody(BaseModel):
    keys: list[str]


class BulkBody(BaseModel):
    action: str
    keys: list[str]
    value: str = ""


class MoveBody(BaseModel):
    key: str
    delta: int


class MetaBody(BaseModel):
    title: str | None = None
    description: str | None = None
    tags: str | None = None
    privacy: str | None = None
    playlist_choice: str | None = None


class PlaylistBody(BaseModel):
    title: str
    privacy: str = "unlisted"


class CreateQueueBody(BaseModel):
    name: str = ""
    vod_folder: str = ""
    account_id: str = ""


class ReorderBody(BaseModel):
    order: list[str]


def create_app() -> FastAPI:
    ctl = Controller()
    app = FastAPI(title="TwitchDVR to YouTube", docs_url=None, redoc_url=None)

    web_password = os.environ.get("WEB_PASSWORD", "")

    @app.middleware("http")
    async def _session_auth(request: Request, call_next):
        # Static files and the login endpoint stay open so the UI can render
        # its own login screen; every API call needs the session cookie.
        if web_password and request.url.path.startswith("/api") \
                and request.url.path != "/api/login":
            cookie = request.cookies.get(SESSION_COOKIE, "")
            if not secrets.compare_digest(cookie, SESSION_TOKEN):
                return JSONResponse(status_code=401,
                                    content={"error": "login required"})
        return await call_next(request)

    @app.middleware("http")
    async def _revalidate_static(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            # Without a Cache-Control header browsers reuse a cached app.js
            # for a while, so right after an update the new page ran old code.
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.post("/api/login")
    def login(body: dict, response: Response):
        if not web_password:
            return {"ok": True}
        supplied = str(body.get("password", ""))
        if not secrets.compare_digest(supplied, web_password):
            raise HTTPException(status_code=403, detail="wrong password")
        response.set_cookie(SESSION_COOKIE, SESSION_TOKEN, httponly=True,
                            samesite="lax", max_age=30 * 24 * 3600)
        return {"ok": True}

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie(SESSION_COOKIE)
        return {"ok": True}

    @app.exception_handler(RuntimeError)
    async def _runtime_error(_request, exc: RuntimeError):
        return JSONResponse(status_code=400, content={"error": str(exc)})

    @app.exception_handler(KeyError)
    async def _key_error(_request, exc: KeyError):
        return JSONResponse(status_code=404, content={"error": str(exc)})

    @app.get("/api/state")
    def state():
        return ctl.snapshot()

    @app.get("/api/events")
    def events(since: int = 0):
        return {"events": ctl.events_since(since)}

    # -------------------------------------------------------------- queues
    @app.post("/api/queues")
    def queues_create(body: CreateQueueBody):
        return ctl.create_queue(body.name, body.vod_folder, body.account_id)

    @app.patch("/api/queues/{queue_id}")
    def queues_update(queue_id: str, patch: dict):
        return ctl.update_queue(queue_id, patch)

    @app.delete("/api/queues/{queue_id}")
    def queues_delete(queue_id: str):
        ctl.remove_queue(queue_id)
        return {"ok": True}

    @app.post("/api/queues/reorder")
    def queues_reorder(body: ReorderBody):
        ctl.reorder_queues(body.order)
        return {"ok": True}

    @app.post("/api/queues/{queue_id}/scan")
    def q_scan(queue_id: str, body: ScanBody):
        folder = (body.folder or ctl.get_queue(queue_id).vod_folder or "").strip()
        if folder and not Path(folder).is_dir():
            raise HTTPException(status_code=400,
                                detail=f"folder does not exist: {folder}")
        ctl.scan_async(queue_id, body.folder)
        return {"started": True}

    @app.patch("/api/queues/{queue_id}/meta/{key}")
    def q_meta(queue_id: str, key: str, body: MetaBody):
        try:
            ctl.update_meta(queue_id, key, body.model_dump(exclude_none=True))
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown vod")
        return {"ok": True}

    @app.post("/api/queues/{queue_id}/bulk")
    def q_bulk(queue_id: str, body: BulkBody):
        return ctl.bulk(queue_id, body.action, body.keys, body.value)

    @app.post("/api/queues/{queue_id}/queue/start")
    def q_start(queue_id: str, body: dict | None = None):
        started = ctl.start_uploads(queue_id, force=bool((body or {}).get("force")))
        return {"ok": True, "started": started}

    @app.post("/api/queues/{queue_id}/queue/retry_failed")
    def q_retry(queue_id: str):
        return {"count": ctl.retry_failed(queue_id)}

    @app.post("/api/queues/{queue_id}/queue/pause")
    def q_pause(queue_id: str):
        ctl.pause_uploads(queue_id)
        return {"ok": True}

    @app.post("/api/queues/{queue_id}/queue/cancel")
    def q_cancel(queue_id: str):
        ctl.cancel_current(queue_id)
        return {"ok": True}

    @app.post("/api/queues/{queue_id}/queue/remove")
    def q_remove(queue_id: str, body: KeysBody):
        ctl.remove_from_queue(queue_id, body.keys)
        return {"ok": True}

    @app.post("/api/queues/{queue_id}/queue/move")
    def q_move(queue_id: str, body: MoveBody):
        ctl.move_in_queue(queue_id, body.key, body.delta)
        return {"ok": True}

    @app.post("/api/queues/{queue_id}/queue/clear_finished")
    def q_clear(queue_id: str):
        ctl.clear_finished(queue_id)
        return {"ok": True}

    @app.get("/api/queues/{queue_id}/history")
    def q_history(queue_id: str):
        return {"history": ctl.history(queue_id),
                "after_upload": ctl.get_queue(queue_id).setting("after_upload", "keep")}

    @app.post("/api/queues/{queue_id}/automation/run")
    def q_automation_run(queue_id: str):
        ctl.automation_run(queue_id)
        return {"ok": True}

    @app.get("/api/queues/{queue_id}/video/{key}/stream")
    def q_video_stream(queue_id: str, key: str):
        """Serve the local video file for the in-browser preview player
        (FileResponse handles HTTP Range, so seeking works)."""
        vod = ctl.get_vod(queue_id, key)
        if vod is None or vod.video_path is None or not vod.video_path.exists():
            raise HTTPException(status_code=404, detail="video file not found")
        suffix = vod.video_path.suffix.lower()
        media = {"mp4": "video/mp4", ".mp4": "video/mp4",
                 ".mkv": "video/x-matroska", ".ts": "video/mp2t"}.get(
            suffix, "application/octet-stream")
        return FileResponse(vod.video_path, media_type=media)

    # ------------------------------------------------------------- settings
    @app.post("/api/settings")
    def settings(patch: dict):
        ctl.update_settings(patch)
        return {"ok": True}

    @app.post("/api/lsdvr/test")
    def lsdvr_test(body: dict):
        return {"message": ctl.lsdvr_test(str(body.get("url", "")).strip(),
                                          str(body.get("password", "")))}

    # ----------------------------------------------------------- yt manager
    # Account-scoped (not queue-scoped) — browsing an already-signed-in
    # channel's existing videos is independent of any one upload queue.
    @app.get("/api/yt/videos")
    def yt_videos(account_id: str):
        return {"videos": ctl.yt_list(account_id)}

    @app.get("/api/yt/video/{video_id}")
    def yt_video(video_id: str, account_id: str):
        return ctl.yt_video(account_id, video_id)

    @app.patch("/api/yt/video/{video_id}")
    def yt_video_update(video_id: str, account_id: str, body: dict):
        ctl.yt_update(account_id, video_id, body)
        return {"ok": True}

    @app.get("/api/yt/video/{video_id}/playlists")
    def yt_video_playlists(video_id: str, account_id: str):
        return {"playlists": ctl.yt_video_playlists(account_id, video_id)}

    @app.post("/api/yt/video/{video_id}/playlists")
    def yt_video_playlist_add(video_id: str, account_id: str, body: dict):
        ctl.yt_playlist_add(account_id, video_id, str(body.get("playlist_id", "")))
        return {"ok": True}

    # Account-scoped (not queue-scoped) — a channel's playlist list is
    # independent of which queue uploads to it. A *queue's* own "always add
    # to playlist X" rule lives in that queue's settings instead.
    @app.get("/api/playlists")
    def playlists_list(account_id: str):
        return {"playlists": ctl.playlists_list(account_id)}

    @app.post("/api/playlists")
    def playlists_create(account_id: str, body: PlaylistBody):
        ctl.playlists_create(account_id, body.title, body.privacy)
        return {"ok": True}

    @app.post("/api/yt/playlist_item/remove")
    def yt_playlist_item_remove(body: dict):
        ctl.yt_playlist_remove(str(body.get("account_id", "")),
                               str(body.get("item_id", "")))
        return {"ok": True}

    @app.post("/api/yt/bulk")
    def yt_bulk(body: dict):
        return ctl.yt_bulk(str(body.get("account_id", "")),
                           str(body.get("action", "")),
                           list(body.get("keys", [])),
                           str(body.get("value", "")))

    # ----------------------------------------------------------------- auth
    @app.post("/api/auth/start")
    def auth_start():
        return ctl.start_sign_in()

    @app.post("/api/auth/remove")
    def auth_remove(body: dict):
        ctl.remove_account(str(body.get("id", "")))
        return {"ok": True}

    @app.post("/api/auth/secret")
    def auth_secret(body: dict):
        client_id = ctl.set_client_secret(str(body.get("content", "")))
        return {"ok": True, "client_id": client_id}

    @app.post("/api/auth/browser")
    def auth_browser():
        return ctl.start_browser_sign_in()

    @app.post("/api/update/apply")
    def update_apply():
        ctl.apply_update()
        return {"ok": True}

    @app.post("/api/update/check")
    def update_check():
        return {"update": ctl.check_updates_now()}

    @app.get("/api/changelog")
    def changelog():
        import sys
        root = Path(getattr(sys, "_MEIPASS",
                            Path(__file__).resolve().parent.parent))
        try:
            return {"changelog": (root / "CHANGELOG.md").read_text(
                encoding="utf-8")}
        except OSError:
            return {"changelog": ""}

    @app.get("/")
    def index():
        # The version in the asset URLs also retires copies that browsers
        # cached before the header above existed.
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        for asset in ("app.js", "style.css"):
            html = html.replace(f'/static/{asset}"',
                                f'/static/{asset}?v={__version__}"')
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
