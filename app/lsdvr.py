"""Delete VODs through LiveStreamDVR's own API instead of the filesystem.

LiveStreamDVR watches its VOD folders. Deleting a folder behind its back can
crash it (on Unraid / network shares: "Unknown system error -116, scandir"),
and it then stays down until someone restarts it. Asking LiveStreamDVR to do
the deletion makes it stop watching that VOD first.

LiveStreamDVR has no API keys. When its web panel has a password, every admin
call needs a session cookie from POST /api/v0/auth/login with that password.
"""
from __future__ import annotations

import json
from pathlib import Path

import requests

TIMEOUT = 30


class LsdvrError(RuntimeError):
    pass


def read_vod_info(json_path: Path) -> dict:
    """The VOD's LiveStreamDVR JSON (uuid, is_capturing, segments, ...)."""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("not a LiveStreamDVR VOD file")
    return data


def segment_index(info: dict, video_path: Path | None) -> int:
    """Position of the uploaded video file in the VOD's segment list."""
    segments = info.get("segments") or []
    names = []
    for seg in segments:
        if isinstance(seg, dict):
            seg = seg.get("filename") or seg.get("basename") or ""
        names.append(Path(str(seg)).name)
    if video_path is not None and video_path.name in names:
        return names.index(video_path.name)
    if len(names) == 1:
        return 0
    raise LsdvrError("can't tell which LiveStreamDVR segment is the uploaded "
                     "video file")


def _body(resp: requests.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {"data": data}


class Client:
    def __init__(self, base_url: str, password: str = ""):
        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise LsdvrError("LiveStreamDVR address is not set in Settings")
        if not base.startswith(("http://", "https://")):
            base = "http://" + base
        self.base = base
        self.password = password or ""
        self.http = requests.Session()
        self._logged_in = False

    def _url(self, path: str) -> str:
        return f"{self.base}/api/v0{path}"

    def _send(self, method: str, path: str, **kwargs) -> requests.Response:
        try:
            return self.http.request(method, self._url(path), timeout=TIMEOUT,
                                     **kwargs)
        except requests.Timeout as exc:
            raise LsdvrError(f"can't reach LiveStreamDVR at {self.base} "
                             "(timed out)") from exc
        except requests.ConnectionError as exc:
            raise LsdvrError(f"can't reach LiveStreamDVR at {self.base} "
                             "(connection failed)") from exc
        except requests.RequestException as exc:
            raise LsdvrError(f"can't reach LiveStreamDVR at {self.base}: "
                             f"{exc}") from exc

    def login(self) -> None:
        if not self.password:
            return      # an open panel needs no session
        resp = self._send("POST", "/auth/login",
                          json={"password": self.password})
        body = _body(resp)
        # 400 comes back both for "already logged in" (authenticated: true)
        # and for "this LiveStreamDVR has no password" — both mean go ahead.
        if resp.status_code == 200 or (resp.status_code == 400
                                       and "authenticated" in body):
            self._logged_in = True
            return
        if resp.status_code == 401:
            raise LsdvrError("LiveStreamDVR rejected the password")
        raise LsdvrError(f"LiveStreamDVR login failed (HTTP "
                         f"{resp.status_code}) — is {self.base} really "
                         "LiveStreamDVR?")

    def _call(self, method: str, path: str, **kwargs) -> dict:
        if not self._logged_in:
            self.login()
        resp = self._send(method, path, **kwargs)
        if resp.status_code == 401 and self.password:
            # the session expired or LiveStreamDVR restarted — log in again
            self._logged_in = False
            self.login()
            resp = self._send(method, path, **kwargs)
        body = _body(resp)
        if resp.status_code == 401:
            raise LsdvrError("LiveStreamDVR wants a password — set it in "
                             "Settings" if not self.password else
                             "LiveStreamDVR refused the session")
        if not resp.ok or body.get("status") == "ERROR":
            raise LsdvrError(body.get("message")
                             or f"HTTP {resp.status_code} from {self.base}")
        return body

    def check(self) -> str:
        """Log in and describe the connection, for the Settings test button."""
        auth = self._call("GET", "/auth/check")
        if auth.get("authentication") and not auth.get("authenticated"):
            raise LsdvrError("LiveStreamDVR did not accept the login")
        how = ("logged in" if auth.get("authentication")
               else "the panel has no password")
        data = self._call("GET", "/channels").get("data")
        if isinstance(data, dict):
            data = data.get("streamer_list")
        if isinstance(data, list):
            return f"connected, {how}, {len(data)} channel(s)"
        return f"connected, {how}"

    def delete_vod(self, uuid: str) -> None:
        """Remove the VOD and its files; LiveStreamDVR forgets it."""
        self._call("DELETE", f"/vod/{uuid}")

    def delete_segment(self, uuid: str, index: int) -> None:
        """Remove one video file but keep the VOD entry, the way
        LiveStreamDVR marks a recording that lives on in the cloud."""
        self._call("POST", f"/vod/{uuid}/delete_segment",
                   params={"segment": index, "keep_entry": "true"})
