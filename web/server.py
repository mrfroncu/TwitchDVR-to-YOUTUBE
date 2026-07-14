"""FastAPI app: JSON API + static frontend for the browser version.

Set the WEB_PASSWORD environment variable (e.g. via an .env file next to
docker-compose.yml) to require signing in. The login form lives in the web
UI itself; a successful login sets an HttpOnly session cookie (valid until
the container restarts).
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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

    @app.get("/api/state")
    def state():
        return ctl.snapshot()

    @app.get("/api/events")
    def events(since: int = 0):
        return {"events": ctl.events_since(since)}

    @app.post("/api/scan")
    def scan(body: ScanBody):
        try:
            count = ctl.scan(body.folder)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"count": count}

    @app.post("/api/settings")
    def settings(patch: dict):
        ctl.update_settings(patch)
        return {"ok": True}

    @app.patch("/api/meta/{key}")
    def meta(key: str, body: MetaBody):
        try:
            ctl.update_meta(key, body.model_dump(exclude_none=True))
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown vod")
        return {"ok": True}

    @app.post("/api/bulk")
    def bulk(body: BulkBody):
        return ctl.bulk(body.action, body.keys, body.value)

    @app.post("/api/queue/start")
    def queue_start(body: dict | None = None):
        ctl.start_uploads(force=bool((body or {}).get("force")))
        return {"ok": True}

    @app.post("/api/queue/retry_failed")
    def queue_retry():
        return {"count": ctl.retry_failed()}

    @app.post("/api/queue/pause")
    def queue_pause():
        ctl.pause_uploads()
        return {"ok": True}

    @app.post("/api/queue/cancel")
    def queue_cancel():
        ctl.cancel_current()
        return {"ok": True}

    @app.post("/api/queue/remove")
    def queue_remove(body: KeysBody):
        ctl.remove_from_queue(body.keys)
        return {"ok": True}

    @app.post("/api/queue/move")
    def queue_move(body: MoveBody):
        ctl.move_in_queue(body.key, body.delta)
        return {"ok": True}

    @app.post("/api/queue/clear_finished")
    def queue_clear():
        ctl.clear_finished()
        return {"ok": True}

    @app.post("/api/auth/start")
    def auth_start():
        return ctl.start_sign_in()

    @app.post("/api/auth/signout")
    def auth_signout():
        ctl.sign_out()
        return {"ok": True}

    @app.post("/api/auth/switch")
    def auth_switch(body: dict):
        ctl.switch_account(str(body.get("id", "")))
        return {"ok": True}

    @app.post("/api/auth/secret")
    def auth_secret(body: dict):
        client_id = ctl.set_client_secret(str(body.get("content", "")))
        return {"ok": True, "client_id": client_id}

    # ------------------------------------------------------------ yt manager
    @app.get("/api/yt/videos")
    def yt_videos():
        return {"videos": ctl.yt_list()}

    @app.get("/api/yt/video/{video_id}")
    def yt_video(video_id: str):
        return ctl.yt_video(video_id)

    @app.patch("/api/yt/video/{video_id}")
    def yt_video_update(video_id: str, body: dict):
        ctl.yt_update(video_id, body)
        return {"ok": True}

    @app.get("/api/yt/video/{video_id}/playlists")
    def yt_video_playlists(video_id: str):
        return {"playlists": ctl.yt_video_playlists(video_id)}

    @app.post("/api/yt/video/{video_id}/playlists")
    def yt_video_playlist_add(video_id: str, body: dict):
        ctl.yt_playlist_add(video_id, str(body.get("playlist_id", "")))
        return {"ok": True}

    @app.post("/api/yt/playlist_item/remove")
    def yt_playlist_item_remove(body: dict):
        ctl.yt_playlist_remove(str(body.get("item_id", "")))
        return {"ok": True}

    @app.post("/api/yt/bulk")
    def yt_bulk(body: BulkBody):
        return ctl.yt_bulk(body.action, body.keys, body.value)

    @app.post("/api/playlists/refresh")
    def playlists_refresh():
        ctl.refresh_playlists()
        return {"ok": True}

    @app.post("/api/playlists/create")
    def playlists_create(body: PlaylistBody):
        ctl.create_playlist(body.title, body.privacy)
        return {"ok": True}

    @app.post("/api/automation/run")
    def automation_run():
        ctl.auto_cycle()
        return {"ok": True}

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
