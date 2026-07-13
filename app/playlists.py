"""YouTube playlist helpers (need the full 'youtube' OAuth scope).

Quota costs: list = 1 unit, create = 50 units, add video = 50 units.
"""
from __future__ import annotations


def list_playlists(service) -> list[dict]:
    """All playlists of the authorized channel: {id, title, privacy, count}."""
    out: list[dict] = []
    token = None
    while True:
        resp = service.playlists().list(
            part="snippet,status,contentDetails", mine=True,
            maxResults=50, pageToken=token).execute()
        for item in resp.get("items", []):
            out.append({
                "id": item["id"],
                "title": item["snippet"]["title"],
                "privacy": item.get("status", {}).get("privacyStatus", ""),
                "count": item.get("contentDetails", {}).get("itemCount", 0),
            })
        token = resp.get("nextPageToken")
        if not token:
            return out


def create_playlist(service, title: str, privacy: str = "unlisted",
                    description: str = "") -> dict:
    item = service.playlists().insert(
        part="snippet,status",
        body={"snippet": {"title": title, "description": description},
              "status": {"privacyStatus": privacy}}).execute()
    return {"id": item["id"], "title": item["snippet"]["title"],
            "privacy": privacy, "count": 0}


def add_to_playlist(service, playlist_id: str, video_id: str) -> None:
    service.playlistItems().insert(
        part="snippet",
        body={"snippet": {"playlistId": playlist_id,
                          "resourceId": {"kind": "youtube#video",
                                         "videoId": video_id}}}).execute()
