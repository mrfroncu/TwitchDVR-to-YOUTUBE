"""Sequential YouTube upload worker.

Runs in a background thread, uploading queue items one at a time. Talks to
the GUI exclusively through an event queue of dicts:

    {"type": "item_status", "key", "status", "detail"}   queued/uploading/done/error/cancelled
    {"type": "progress", "key", "pct", "speed_bps", "eta_s"}
    {"type": "log", "text"}
    {"type": "worker_done", "reason"}                     finished/paused/quota

Uploads speak Google's resumable-upload protocol directly with `requests`
(one streaming PUT for the whole file, resumed from the committed offset on
connection loss). googleapiclient's chunked next_chunk() loop is avoided on
purpose: it caps out around 10 MB/s in practice (see
github.com/googleapis/google-api-python-client issues #625 / #793), while a
single streamed request — what browsers do — saturates fast connections.
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field

from google.auth.transport.requests import AuthorizedSession

from .scanner import Vod

UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
RETRIABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 10
QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded",
                 "rateLimitExceeded", "userRateLimitExceeded"}


@dataclass
class QueueItem:
    vod: Vod
    title: str
    description: str
    tags: list[str]
    privacy: str
    category_id: str
    recording_date: str | None
    notify_subscribers: bool
    made_for_kids: bool
    # None, or {"type": "id"|"name", "value": ..., "title": optional display}
    playlist: dict | None = None
    status: str = "queued"      # queued | uploading | done | error | cancelled
    detail: str = ""
    video_id: str | None = None
    progress: float = 0.0

    @property
    def key(self) -> str:
        return self.vod.key


class QuotaExceeded(Exception):
    pass


class UploadCancelled(Exception):
    pass


class UploadHttpError(Exception):
    """Non-retriable HTTP error from the upload endpoint."""

    def __init__(self, status_code: int, text: str):
        super().__init__(f"HTTP {status_code}: {text[:300]}")
        self.status_code = status_code
        self.text = text


class _ProgressReader:
    """File-like view of a byte range with throttled progress + cancel.

    `len` is what requests uses for the Content-Length header.
    """

    def __init__(self, fh, start: int, end: int, callback, cancel_event):
        self._fh = fh
        self._pos = start
        self._end = end
        self._callback = callback
        self._cancel = cancel_event
        self._last_report = 0.0
        self.len = end - start
        fh.seek(start)

    def read(self, n: int = -1) -> bytes:
        if self._cancel.is_set():
            raise UploadCancelled()
        remaining = self._end - self._pos
        if remaining <= 0:
            return b""
        if n is None or n < 0 or n > remaining:
            n = remaining
        data = self._fh.read(n)
        self._pos += len(data)
        now = time.monotonic()
        if now - self._last_report >= 0.5 or self._pos >= self._end:
            self._last_report = now
            self._callback(self._pos)
        return data


def verify_video(service, video_id: str) -> tuple[bool, str]:
    """Check on YouTube that an uploaded video exists and is not failed/rejected.

    Returns (ok, detail). Costs 1 API quota unit.
    """
    resp = service.videos().list(part="status,processingDetails",
                                 id=video_id).execute()
    items = resp.get("items") or []
    if not items:
        return False, "video not found on YouTube"
    status = items[0].get("status", {})
    upload_status = status.get("uploadStatus", "")
    if upload_status == "processed":
        return True, "processed"
    if upload_status == "uploaded":
        proc = (items[0].get("processingDetails") or {}).get("processingStatus", "")
        return True, "uploaded, still processing" + (f" ({proc})" if proc else "")
    reason = status.get("failureReason") or status.get("rejectionReason") or ""
    detail = f"uploadStatus={upload_status or 'unknown'}"
    if reason:
        detail += f", reason={reason}"
    return False, detail


class UploadWorker(threading.Thread):
    def __init__(self, credentials, items: list[QueueItem], events,
                 daily_limit: int = 0, count_recent=None):
        super().__init__(daemon=True, name="upload-worker")
        self._credentials = credentials
        self._items = items
        self._events = events
        self._daily_limit = max(0, int(daily_limit))
        self._count_recent = count_recent   # callable -> uploads in last ~24h
        self._session: AuthorizedSession | None = None
        self.pause_requested = threading.Event()   # finish current item, then stop
        self.cancel_current = threading.Event()    # abort the in-flight item
        self._playlists_cache: list[dict] | None = None

    # ------------------------------------------------------------------ api --
    def run(self) -> None:
        from .auth import build_service
        reason = "finished"
        try:
            service = build_service(self._credentials)
            self._session = AuthorizedSession(self._credentials)
        except Exception as exc:
            self._emit({"type": "log", "text": f"Could not build YouTube client: {exc}"})
            self._emit({"type": "worker_done", "reason": "error"})
            return

        detail = ""
        while True:
            if self.pause_requested.is_set():
                reason = "paused"
                break
            # Re-scan the live list each time so the GUI can reorder/remove/add
            # pending items while uploads are running.
            item = next((i for i in self._items if i.status == "queued"), None)
            if item is None:
                reason = "finished"
                break
            if self._daily_limit and self._count_recent and \
                    self._count_recent() >= self._daily_limit:
                reason = "daily_limit"
                self._emit({"type": "log",
                            "text": f"Configured daily upload limit "
                                    f"({self._daily_limit}/24h) reached — "
                                    "stopping before YouTube complains."})
                break
            self.cancel_current.clear()
            self._set_status(item, "uploading", "starting…")
            try:
                video_id = self._upload_one(service, item)
                item.video_id = video_id
                self._set_status(item, "verifying", "confirming video on YouTube…")
                ok, detail = self._verify_with_retry(service, video_id)
                url = f"https://youtu.be/{video_id}"
                if ok is True:
                    self._set_status(item, "done", f"{url} — verified ({detail})",
                                     verified=True)
                    self._emit({"type": "log",
                                "text": f"Uploaded and verified '{item.title}' -> {url} ({detail})"})
                    self._handle_playlist(service, item)
                elif ok is None:
                    self._set_status(item, "done",
                                     f"{url} — uploaded, verification unavailable ({detail})",
                                     verified=False)
                    self._emit({"type": "log",
                                "text": f"Uploaded '{item.title}' -> {url}, but could not "
                                        f"verify it: {detail}"})
                    self._handle_playlist(service, item)
                else:
                    self._set_status(item, "error", f"upload verification failed: {detail}")
                    self._emit({"type": "log",
                                "text": f"Verification FAILED for '{item.title}' ({url}): {detail}"})
            except UploadCancelled:
                self._set_status(item, "cancelled", "cancelled by user")
            except QuotaExceeded as exc:
                detail = str(exc)
                self._set_status(item, "queued", "waiting (limit)")
                if "uploadLimitExceeded" in detail:
                    text = ("YouTube says the channel exceeded its upload limit "
                            "(rolling ~24h window). The queue keeps the video and "
                            "will retry after the cooldown.")
                else:
                    text = (f"YouTube API quota exhausted ({detail}). Uploads cost "
                            "1600 units each, default daily quota is 10,000. It "
                            "resets at midnight Pacific time.")
                self._emit({"type": "log", "text": text})
                reason = "quota"
                break
            except Exception as exc:
                self._set_status(item, "error", str(exc)[:300])
                self._emit({"type": "log", "text": f"Upload failed for '{item.title}': {exc}"})
        self._emit({"type": "worker_done", "reason": reason, "detail": detail})

    # ------------------------------------------------------------- internals --
    def _upload_one(self, service, item: QueueItem) -> str:
        vod = item.vod
        if vod.video_path is None or not vod.video_path.exists():
            raise FileNotFoundError(f"video file missing: {vod.video_path}")

        body = {
            "snippet": {
                "title": item.title,
                "description": item.description,
                "tags": item.tags,
                "categoryId": item.category_id,
            },
            "status": {
                "privacyStatus": item.privacy,
                "selfDeclaredMadeForKids": item.made_for_kids,
            },
        }
        parts = "snippet,status"
        if item.recording_date:
            body["recordingDetails"] = {"recordingDate": item.recording_date}
            parts += ",recordingDetails"

        try:
            return self._run_resumable(item, body, parts)
        except UploadHttpError as err:
            # Some accounts reject recordingDetails; retry without it once.
            if err.status_code == 400 and "recordingDetails" in parts and \
                    "recording" in err.text.lower():
                body.pop("recordingDetails", None)
                self._emit({"type": "log",
                            "text": "recordingDate rejected by API, retrying without it"})
                return self._run_resumable(item, body, "snippet,status")
            raise

    def _raise_for_response(self, resp) -> None:
        reasons: set[str] = set()
        try:
            for detail in resp.json().get("error", {}).get("errors", []):
                if detail.get("reason"):
                    reasons.add(detail["reason"])
        except ValueError:
            pass
        # uploadLimitExceeded arrives as HTTP 400, quotaExceeded as 403 —
        # treat quota-family reasons the same regardless of status code.
        if reasons & QUOTA_REASONS:
            raise QuotaExceeded(", ".join(sorted(reasons & QUOTA_REASONS)))
        raise UploadHttpError(resp.status_code, resp.text or "")

    def _committed_offset(self, session_uri: str, total: int):
        """Ask the upload session how many bytes it has. Returns
        (offset, final_response_or_None) — a final response means the upload
        actually completed before the connection dropped."""
        resp = self._session.put(
            session_uri, headers={"Content-Range": f"bytes */{total}"},
            timeout=(30, 60))
        if resp.status_code in (200, 201):
            return total, resp
        if resp.status_code == 308:
            rng = resp.headers.get("Range", "")
            if "-" in rng:
                try:
                    return int(rng.rsplit("-", 1)[-1]) + 1, None
                except ValueError:
                    pass
            return 0, None
        self._raise_for_response(resp)

    def _run_resumable(self, item: QueueItem, body: dict, parts: str) -> str:
        """One streaming PUT for the whole file on a resumable session,
        resumed from the server's committed offset after connection loss."""
        vod = item.vod
        total = vod.video_path.stat().st_size
        params = {
            "uploadType": "resumable",
            "part": parts,
            "notifySubscribers": "true" if item.notify_subscribers else "false",
        }
        resp = self._session.post(
            UPLOAD_URL, params=params, json=body,
            headers={"X-Upload-Content-Length": str(total),
                     "X-Upload-Content-Type": "video/*"},
            timeout=(30, 60))
        if resp.status_code != 200:
            self._raise_for_response(resp)
        session_uri = resp.headers["Location"]

        def finish(resp) -> str:
            video_id = resp.json().get("id")
            if not video_id:
                raise RuntimeError(f"unexpected API response: {resp.text[:300]}")
            self._emit({"type": "progress", "key": item.key, "pct": 100.0,
                        "speed_bps": 0, "eta_s": 0})
            return video_id

        offset = 0
        retry = 0
        with open(vod.video_path, "rb") as fh:
            while True:
                if self.cancel_current.is_set():
                    raise UploadCancelled()

                attempt_start = time.monotonic()
                attempt_base = offset

                def report(sent: int) -> None:
                    elapsed = max(0.001, time.monotonic() - attempt_start)
                    speed = (sent - attempt_base) / elapsed
                    eta = (total - sent) / speed if speed > 0 else 0
                    self._emit({"type": "progress", "key": item.key,
                                "pct": sent / total * 100 if total else 0,
                                "speed_bps": speed, "eta_s": eta})

                reader = _ProgressReader(fh, offset, total, report,
                                         self.cancel_current)
                try:
                    resp = self._session.put(
                        session_uri, data=reader,
                        headers={"Content-Range":
                                 f"bytes {offset}-{total - 1}/{total}",
                                 "Content-Type": "video/*"},
                        timeout=(30, 120))
                except UploadCancelled:
                    raise
                except Exception as exc:
                    retry = self._backoff(retry, repr(exc))
                    offset, final = self._committed_offset(session_uri, total)
                    if final is not None:
                        return finish(final)
                    continue

                if resp.status_code in (200, 201):
                    return finish(resp)
                if resp.status_code == 308:
                    offset, final = self._committed_offset(session_uri, total)
                    if final is not None:
                        return finish(final)
                    continue
                if resp.status_code in RETRIABLE_STATUS:
                    retry = self._backoff(retry, f"HTTP {resp.status_code}")
                    offset, final = self._committed_offset(session_uri, total)
                    if final is not None:
                        return finish(final)
                    continue
                self._raise_for_response(resp)

    def _backoff(self, retry: int, why: str) -> int:
        retry += 1
        if retry > MAX_RETRIES:
            raise RuntimeError(f"gave up after {MAX_RETRIES} retries ({why})")
        delay = min(60.0, (2 ** retry) * 0.5) + random.random()
        self._emit({"type": "log",
                    "text": f"Transient upload error ({why}), retry {retry}/{MAX_RETRIES} "
                            f"in {delay:.0f}s"})
        # Sleep in small steps so cancel stays responsive
        end = time.monotonic() + delay
        while time.monotonic() < end:
            if self.cancel_current.is_set():
                raise UploadCancelled()
            time.sleep(0.2)
        return retry

    def _handle_playlist(self, service, item: QueueItem) -> None:
        """Add the uploaded video to its playlist (finding or creating it).

        Failures are logged but never fail the queue item — the video is
        already safely on YouTube at this point.
        """
        spec = item.playlist
        if not spec or not item.video_id:
            return
        from . import playlists as pl
        try:
            if spec.get("type") == "id":
                playlist_id = spec["value"]
                title = spec.get("title") or playlist_id
            else:
                name = spec["value"]
                if self._playlists_cache is None:
                    self._playlists_cache = pl.list_playlists(service)
                found = next((p for p in self._playlists_cache
                              if p["title"].strip().lower() == name.strip().lower()),
                             None)
                if found is None:
                    privacy = "public" if item.privacy == "public" else "unlisted"
                    found = pl.create_playlist(service, name, privacy)
                    self._playlists_cache.append(found)
                    self._emit({"type": "log",
                                "text": f"Created playlist '{found['title']}' ({privacy})"})
                playlist_id, title = found["id"], found["title"]
            pl.add_to_playlist(service, playlist_id, item.video_id)
            item.detail += f" · playlist: {title}"
            self._emit({"type": "item_detail", "key": item.key, "detail": item.detail})
            self._emit({"type": "log",
                        "text": f"Added '{item.title}' to playlist '{title}'"})
        except Exception as exc:
            self._emit({"type": "log",
                        "text": f"Could not add '{item.title}' to a playlist: "
                                f"{str(exc)[:250]}"})

    def _verify_with_retry(self, service, video_id: str) -> tuple[bool | None, str]:
        """(True/False, detail) from verify_video; (None, error) if the check
        itself kept failing (network/quota) — the upload still succeeded."""
        last = ""
        for attempt in range(3):
            try:
                return verify_video(service, video_id)
            except Exception as exc:
                last = str(exc)[:200]
                if attempt < 2:
                    time.sleep(3)
        return None, last

    def _set_status(self, item: QueueItem, status: str, detail: str,
                    verified: bool | None = None) -> None:
        item.status = status
        item.detail = detail
        event = {"type": "item_status", "key": item.key,
                 "status": status, "detail": detail, "video_id": item.video_id}
        if verified is not None:
            event["verified"] = verified
        self._emit(event)

    def _emit(self, event: dict) -> None:
        self._events.put(event)
