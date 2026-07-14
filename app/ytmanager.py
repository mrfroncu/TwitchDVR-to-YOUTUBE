"""Channel video manager: list and manage the videos already on YouTube.

Quota costs: listing ~2 units per 50 videos; privacy update / playlist add /
delete cost ~50 units each.
"""
from __future__ import annotations

import re

_DUR = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def _fmt_duration(iso: str) -> str:
    m = _DUR.fullmatch(iso or "")
    if not m:
        return ""
    h, mi, s = (int(g or 0) for g in m.groups())
    return f"{h}:{mi:02d}:{s:02d}" if h else f"{mi:02d}:{s:02d}"


def list_channel_videos(service, limit: int = 500) -> list[dict]:
    """Videos of the authorized channel (via its uploads playlist)."""
    ch = service.channels().list(part="contentDetails", mine=True).execute()
    items = ch.get("items") or []
    if not items:
        return []
    uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    videos: list[dict] = []
    token = None
    while len(videos) < limit:
        resp = service.playlistItems().list(
            part="contentDetails", playlistId=uploads,
            maxResults=50, pageToken=token).execute()
        ids = [it["contentDetails"]["videoId"] for it in resp.get("items", [])]
        if ids:
            details = service.videos().list(
                part="snippet,status,statistics,contentDetails",
                id=",".join(ids), maxResults=50).execute()
            for v in details.get("items", []):
                videos.append({
                    "id": v["id"],
                    "title": v["snippet"]["title"],
                    "published": (v["snippet"].get("publishedAt") or "")[:10],
                    "privacy": v["status"].get("privacyStatus", ""),
                    "upload_status": v["status"].get("uploadStatus", ""),
                    "views": int(v.get("statistics", {}).get("viewCount", 0) or 0),
                    "duration": _fmt_duration(
                        v.get("contentDetails", {}).get("duration", "")),
                })
        token = resp.get("nextPageToken")
        if not token:
            break
    return videos


def get_video(service, video_id: str) -> dict:
    """Full editable metadata of one video."""
    resp = service.videos().list(part="snippet,status", id=video_id).execute()
    items = resp.get("items") or []
    if not items:
        raise RuntimeError(f"video {video_id} not found")
    snippet, status = items[0]["snippet"], items[0]["status"]
    return {
        "id": video_id,
        "title": snippet.get("title", ""),
        "description": snippet.get("description", ""),
        "tags": snippet.get("tags") or [],
        "category_id": snippet.get("categoryId", "20"),
        "privacy": status.get("privacyStatus", "private"),
    }


def update_video(service, video_id: str, *, title: str, description: str,
                 tags: list[str], category_id: str, privacy: str) -> None:
    """Fetch-modify-update of snippet+status (the API replaces the whole
    snippet, so unspecified fields must be carried over)."""
    resp = service.videos().list(part="snippet,status", id=video_id).execute()
    items = resp.get("items") or []
    if not items:
        raise RuntimeError(f"video {video_id} not found")
    snippet, status = items[0]["snippet"], items[0]["status"]
    snippet["title"] = title
    snippet["description"] = description
    snippet["tags"] = tags
    snippet["categoryId"] = category_id
    status["privacyStatus"] = privacy
    service.videos().update(
        part="snippet,status",
        body={"id": video_id, "snippet": snippet, "status": status}).execute()


def video_playlists(service, channel_playlists: list[dict],
                    video_id: str) -> list[dict]:
    """Which of the channel's playlists contain the video.
    Returns [{'playlist_id', 'title', 'item_id'}] (item_id allows removal)."""
    out = []
    for playlist in channel_playlists:
        resp = service.playlistItems().list(
            part="id", playlistId=playlist["id"], videoId=video_id,
            maxResults=1).execute()
        items = resp.get("items") or []
        if items:
            out.append({"playlist_id": playlist["id"],
                        "title": playlist["title"],
                        "item_id": items[0]["id"]})
    return out


def remove_from_playlist(service, playlist_item_id: str) -> None:
    service.playlistItems().delete(id=playlist_item_id).execute()


def set_privacy(service, video_id: str, privacy: str) -> None:
    """Fetch-modify-update so other status fields aren't clobbered."""
    resp = service.videos().list(part="status", id=video_id).execute()
    items = resp.get("items") or []
    if not items:
        raise RuntimeError(f"video {video_id} not found")
    status = items[0]["status"]
    status["privacyStatus"] = privacy
    service.videos().update(part="status",
                            body={"id": video_id, "status": status}).execute()


def delete_video(service, video_id: str) -> None:
    service.videos().delete(id=video_id).execute()
