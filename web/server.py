"""FastAPI app: JSON API + static frontend for the browser version.

Set the WEB_PASSWORD environment variable (e.g. via an .env file next to
docker-compose.yml) to require HTTP Basic authentication for everything.
"""
from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .state import Controller

STATIC_DIR = Path(__file__).parent / "static"


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
    async def _basic_auth(request: Request, call_next):
        if web_password:
            header = request.headers.get("authorization", "")
            ok = False
            if header.startswith("Basic "):
                try:
                    decoded = base64.b64decode(header[6:]).decode("utf-8")
                    supplied = decoded.split(":", 1)[1] if ":" in decoded else decoded
                    ok = secrets.compare_digest(supplied, web_password)
                except Exception:
                    ok = False
            if not ok:
                return Response(
                    status_code=401, content="Authentication required",
                    headers={"WWW-Authenticate":
                             'Basic realm="TwitchDVR to YouTube"'})
        return await call_next(request)

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
