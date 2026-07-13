"""Sequential YouTube upload worker.

Runs in a background thread, uploading queue items one at a time with
resumable chunked uploads. Talks to the GUI exclusively through an event
queue of dicts:

    {"type": "item_status", "key", "status", "detail"}   queued/uploading/done/error/cancelled
    {"type": "progress", "key", "pct", "speed_bps", "eta_s"}
    {"type": "log", "text"}
    {"type": "worker_done", "reason"}                     finished/paused/quota
"""
from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass, field

import httplib2
from googleapiclient.errors import HttpError, ResumableUploadError
from googleapiclient.http import MediaFileUpload

from .scanner import Vod

RETRIABLE_STATUS = {429, 500, 502, 503, 504}
RETRIABLE_EXCEPTIONS = (httplib2.HttpLib2Error, OSError, ConnectionError, TimeoutError)
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


def _http_error_reasons(err: HttpError) -> set[str]:
    reasons: set[str] = set()
    try:
        payload = json.loads(err.content.decode("utf-8"))
        for detail in payload.get("error", {}).get("errors", []):
            if detail.get("reason"):
                reasons.add(detail["reason"])
    except (ValueError, AttributeError, UnicodeDecodeError):
        pass
    return reasons


class UploadWorker(threading.Thread):
    def __init__(self, credentials, items: list[QueueItem], events, chunk_mb: int = 64):
        super().__init__(daemon=True, name="upload-worker")
        self._credentials = credentials
        self._items = items
        self._events = events
        self._chunk_bytes = max(1, min(1024, int(chunk_mb))) * 1024 * 1024
        # round to the 256 KiB multiple the API requires
        self._chunk_bytes -= self._chunk_bytes % (256 * 1024)
        self.pause_requested = threading.Event()   # finish current item, then stop
        self.cancel_current = threading.Event()    # abort the in-flight item

    # ------------------------------------------------------------------ api --
    def run(self) -> None:
        from .auth import build_service
        reason = "finished"
        try:
            service = build_service(self._credentials)
        except Exception as exc:
            self._emit({"type": "log", "text": f"Could not build YouTube client: {exc}"})
            self._emit({"type": "worker_done", "reason": "error"})
            return

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
                elif ok is None:
                    self._set_status(item, "done",
                                     f"{url} — uploaded, verification unavailable ({detail})",
                                     verified=False)
                    self._emit({"type": "log",
                                "text": f"Uploaded '{item.title}' -> {url}, but could not "
                                        f"verify it: {detail}"})
                else:
                    self._set_status(item, "error", f"upload verification failed: {detail}")
                    self._emit({"type": "log",
                                "text": f"Verification FAILED for '{item.title}' ({url}): {detail}"})
            except UploadCancelled:
                self._set_status(item, "cancelled", "cancelled by user")
            except QuotaExceeded as exc:
                self._set_status(item, "queued", "waiting (quota)")
                self._emit({"type": "log", "text": (
                    f"YouTube API quota exhausted: {exc}. Uploads cost 1600 units each and the "
                    "default daily quota is 10,000 (~6 uploads/day). Quota resets at midnight "
                    "Pacific time — press Start again after that.")})
                reason = "quota"
                break
            except Exception as exc:
                self._set_status(item, "error", str(exc)[:300])
                self._emit({"type": "log", "text": f"Upload failed for '{item.title}': {exc}"})
        self._emit({"type": "worker_done", "reason": reason})

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
            return self._run_resumable(service, item, body, parts)
        except HttpError as err:
            # Some accounts reject recordingDetails; retry without it once.
            if err.resp.status == 400 and "recordingDetails" in parts and \
                    "recording" in str(err).lower():
                body.pop("recordingDetails", None)
                self._emit({"type": "log",
                            "text": "recordingDate rejected by API, retrying without it"})
                return self._run_resumable(service, item, body, "snippet,status")
            raise

    def _run_resumable(self, service, item: QueueItem, body: dict, parts: str) -> str:
        vod = item.vod
        media = MediaFileUpload(str(vod.video_path), chunksize=self._chunk_bytes,
                                resumable=True)
        request = service.videos().insert(
            part=parts,
            body=body,
            media_body=media,
            notifySubscribers=item.notify_subscribers,
        )

        total = vod.size_bytes or vod.video_path.stat().st_size
        start_time = time.monotonic()
        response = None
        retry = 0
        while response is None:
            if self.cancel_current.is_set():
                raise UploadCancelled()
            try:
                status, response = request.next_chunk()
                retry = 0
                if status:
                    sent = status.resumable_progress
                    elapsed = max(0.001, time.monotonic() - start_time)
                    speed = sent / elapsed
                    eta = (total - sent) / speed if speed > 0 else 0
                    self._emit({"type": "progress", "key": item.key,
                                "pct": sent / total * 100 if total else 0,
                                "speed_bps": speed, "eta_s": eta})
            except HttpError as err:
                reasons = _http_error_reasons(err)
                if err.resp.status == 403 and reasons & QUOTA_REASONS:
                    raise QuotaExceeded(", ".join(sorted(reasons)) or "403") from err
                if err.resp.status in RETRIABLE_STATUS:
                    retry = self._backoff(retry, f"HTTP {err.resp.status}")
                else:
                    raise
            except ResumableUploadError as err:
                reasons = _http_error_reasons(err)
                if reasons & QUOTA_REASONS:
                    raise QuotaExceeded(", ".join(sorted(reasons))) from err
                raise
            except RETRIABLE_EXCEPTIONS as err:
                retry = self._backoff(retry, repr(err))

        video_id = response.get("id")
        if not video_id:
            raise RuntimeError(f"unexpected API response: {response}")
        self._emit({"type": "progress", "key": item.key, "pct": 100.0,
                    "speed_bps": 0, "eta_s": 0})
        return video_id

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
