"""Scan a LiveStreamDVR VOD folder and build YouTube upload metadata.

Expected layout (one subfolder per stream):
    <login>_<date>Z_<captureid>/
        <base>.json                  LiveStreamDVR metadata (chapters, titles, dates)
        <base>.mp4 or <base>.ts      the video
        <base>-ffmpeg-chapters.txt   optional; used for the streamer display name
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

FOLDER_RE = re.compile(
    r"^(?P<login>.+)_(?P<date>\d{4}-\d{2}-\d{2}T\d{2}_\d{2}_\d{2}Z)_(?P<capture>\d+)$"
)

MAX_TITLE_LEN = 100
MAX_DESC_LEN = 4900     # YouTube hard limit is 5000; keep headroom
MAX_TAGS_LEN = 480      # YouTube hard limit is 500 across all tags
MIN_CHAPTER_SECONDS = 10  # YouTube ignores chapters shorter than 10s


@dataclass
class Chapter:
    offset: float          # seconds from video start
    title: str
    game: str


@dataclass
class Vod:
    key: str               # folder name, unique id
    folder: Path
    json_path: Path
    video_path: Path | None
    streamer_login: str
    streamer_name: str
    stream_title: str
    started_at: datetime | None
    duration: float        # seconds, 0 if unknown
    size_bytes: int
    chapters: list[Chapter] = field(default_factory=list)
    games: list[str] = field(default_factory=list)
    twitch_vod_id: str | None = None
    is_finalized: bool = True
    problems: list[str] = field(default_factory=list)

    @property
    def date_str(self) -> str:
        return self.started_at.strftime("%Y-%m-%d") if self.started_at else ""


def _parse_iso(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _read_artist(ffmpeg_chapters: Path) -> str:
    """Streamer display name from the 'artist=' line of the ffmeta file."""
    try:
        with open(ffmpeg_chapters, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("artist="):
                    return line[len("artist="):].strip()
                if line.startswith("[CHAPTER]"):
                    break
    except OSError:
        pass
    return ""


def _load_dvr_json(path: Path) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    # Heuristic: LiveStreamDVR vod jsons carry chapters and/or started_at
    if "chapters" in data or "started_at" in data or data.get("type") == "twitch":
        return data
    return None


def scan_vod_dir(sub: Path) -> Vod | None:
    """Parse a single VOD subfolder; returns None if it doesn't look like one."""
    data = None
    json_path = None
    # Prefer the json named after the folder, then any other parsable one
    candidates = sorted(sub.glob("*.json"),
                        key=lambda p: (p.stem != sub.name, p.name))
    for cand in candidates:
        data = _load_dvr_json(cand)
        if data is not None:
            json_path = cand
            break
    if data is None or json_path is None:
        return None

    base = json_path.stem
    video_path = None
    for ext in (".mp4", ".ts", ".mkv"):
        p = sub / (base + ext)
        if p.exists():
            video_path = p
            break
    if video_path is None:
        for pattern in ("*.mp4", "*.ts", "*.mkv"):
            found = sorted(sub.glob(pattern))
            if found:
                video_path = found[0]
                break

    m = FOLDER_RE.match(sub.name) or FOLDER_RE.match(base)
    login = m.group("login") if m else ""
    streamer_name = _read_artist(sub / f"{base}-ffmpeg-chapters.txt") or login

    started_at = _parse_iso(data.get("started_at")) or _parse_iso(data.get("created_at"))

    # Zero point for chapter offsets: when capture actually began
    zero = (_parse_iso(data.get("capture_started"))
            or _parse_iso(data.get("created_at")))

    raw_chapters = data.get("chapters") or []
    chapters: list[Chapter] = []
    for ch in raw_chapters:
        ch_start = _parse_iso(ch.get("started_at"))
        if zero is None:
            zero = ch_start
        offset = max(0.0, (ch_start - zero).total_seconds()) if ch_start and zero else 0.0
        chapters.append(Chapter(
            offset=offset,
            title=(ch.get("title") or "").strip(),
            game=(ch.get("game_name") or "").strip(),
        ))
    chapters.sort(key=lambda c: c.offset)
    if chapters:
        chapters[0].offset = 0.0

    duration = 0.0
    if isinstance(data.get("duration"), (int, float)):
        duration = float(data["duration"])
    elif isinstance(data.get("video_metadata"), dict):
        vm = data["video_metadata"]
        if isinstance(vm.get("duration"), (int, float)):
            duration = float(vm["duration"])

    games: list[str] = []
    for ch in chapters:
        if ch.game and ch.game not in games:
            games.append(ch.game)

    stream_title = (data.get("twitch_vod_title")
                    or data.get("external_vod_title")
                    or (chapters[0].title if chapters else "")
                    or sub.name).strip()

    vod = Vod(
        key=sub.name,
        folder=sub,
        json_path=json_path,
        video_path=video_path,
        streamer_login=login,
        streamer_name=streamer_name,
        stream_title=stream_title,
        started_at=started_at,
        duration=duration,
        size_bytes=video_path.stat().st_size if video_path else 0,
        chapters=chapters,
        games=games,
        twitch_vod_id=str(data.get("twitch_vod_id") or data.get("external_vod_id") or "") or None,
        is_finalized=bool(data.get("is_finalized", True)),
    )
    if video_path is None:
        vod.problems.append("no video file")
    if not vod.is_finalized:
        vod.problems.append("not finalized")
    if video_path is not None and video_path.suffix.lower() == ".ts":
        vod.problems.append("raw .ts capture")
    return vod


def scan_folder(root: Path) -> list[Vod]:
    """Scan every subfolder of `root`; also accepts `root` itself being a VOD dir."""
    vods: list[Vod] = []
    if not root.is_dir():
        return vods
    single = scan_vod_dir(root)
    if single is not None:
        return [single]
    for sub in sorted(root.iterdir()):
        if sub.is_dir():
            vod = scan_vod_dir(sub)
            if vod is not None:
                vods.append(vod)
    vods.sort(key=lambda v: (v.started_at or datetime.min.replace(tzinfo=timezone.utc)))
    return vods


# ---------------------------------------------------------------- metadata --

def _fmt_offset(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}"


def sanitize_title(title: str) -> str:
    title = title.replace("<", "").replace(">", "").strip()
    if len(title) > MAX_TITLE_LEN:
        title = title[:MAX_TITLE_LEN - 1].rstrip() + "…"
    return title or "Untitled stream"


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def build_title(vod: Vod, template: str) -> str:
    values = _SafeDict(
        title=vod.stream_title,
        streamer=vod.streamer_name or vod.streamer_login,
        login=vod.streamer_login,
        date=vod.date_str,
        game=vod.games[0] if vod.games else "",
        games=", ".join(vod.games),
    )
    try:
        raw = template.format_map(values)
    except (ValueError, IndexError):
        raw = vod.stream_title
    return sanitize_title(raw)


def chapter_lines(vod: Vod) -> list[str]:
    """YouTube chapter lines: first at 00:00, merged, >=10s each.

    If the stream title never changed the label is just the game name;
    otherwise 'Stream title (Game)'.
    """
    if not vod.chapters:
        return []
    distinct_titles = {c.title for c in vod.chapters if c.title}
    use_game_only = len(distinct_titles) <= 1

    labeled: list[tuple[float, str]] = []
    for ch in vod.chapters:
        if use_game_only:
            label = ch.game or ch.title or "Stream"
        else:
            label = f"{ch.title} ({ch.game})" if ch.game else (ch.title or "Stream")
        if labeled and labeled[-1][1] == label:
            continue  # merge consecutive chapters with identical labels
        labeled.append((ch.offset, label))

    # Enforce YouTube's 10s minimum: drop chapters starting <10s after the
    # previously kept one, and any chapter within 10s of the end.
    filtered: list[tuple[float, str]] = [(0.0, labeled[0][1])]
    for offset, label in labeled[1:]:
        if offset - filtered[-1][0] < MIN_CHAPTER_SECONDS:
            continue
        if vod.duration and vod.duration - offset < MIN_CHAPTER_SECONDS:
            continue
        if label == filtered[-1][1]:
            continue
        filtered.append((offset, label))

    return [f"{_fmt_offset(off)} {label}" for off, label in filtered]


def build_description(vod: Vod) -> str:
    lines: list[str] = [vod.stream_title, ""]
    who = vod.streamer_name or vod.streamer_login or "unknown"
    when = f" on {vod.date_str}" if vod.date_str else ""
    lines.append(f"Streamed live by {who} on Twitch{when}.")
    if vod.streamer_login:
        lines.append(f"https://www.twitch.tv/{vod.streamer_login}")
    if vod.duration:
        lines.append(f"Stream length: {_fmt_duration(vod.duration)}")
    if vod.games:
        lines.append("Games: " + ", ".join(vod.games))

    ch_lines = chapter_lines(vod)
    if ch_lines:
        lines += ["", "Chapters:"]
        lines += ch_lines

    if vod.twitch_vod_id:
        lines += ["", f"Original Twitch VOD ID: {vod.twitch_vod_id}"]

    desc = "\n".join(lines).replace("<", "").replace(">", "")
    if len(desc) > MAX_DESC_LEN:
        desc = desc[:MAX_DESC_LEN]
    return desc


def build_tags(vod: Vod) -> list[str]:
    candidates = [vod.streamer_name, vod.streamer_login, "Twitch", "VOD",
                  "stream", "gameplay", *vod.games]
    tags: list[str] = []
    seen: set[str] = set()
    total = 0
    for tag in candidates:
        tag = (tag or "").strip()
        if not tag or len(tag) > 100:
            continue
        low = tag.lower()
        if low in seen:
            continue
        # Each tag also implicitly costs its quotes when it contains spaces
        cost = len(tag) + (2 if " " in tag else 0) + 1
        if total + cost > MAX_TAGS_LEN:
            break
        seen.add(low)
        tags.append(tag)
        total += cost
    return tags


def recording_date(vod: Vod) -> str | None:
    """RFC3339 timestamp for YouTube's recordingDetails.recordingDate."""
    if not vod.started_at:
        return None
    return vod.started_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
