"""Configuration and persistent state.

Stored per platform: %APPDATA%\\TwitchDVR-to-YouTube on Windows,
~/Library/Application Support/TwitchDVR-to-YouTube on macOS,
$XDG_CONFIG_HOME/TwitchDVR-to-YouTube elsewhere.
"""
import json
import os
import sys
from pathlib import Path

APP_NAME = "TwitchDVR-to-YouTube"


def _app_dir() -> Path:
    override = os.environ.get("APP_DIR")   # set in Docker to the /config volume
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME",
                               str(Path.home() / ".config"))) / APP_NAME


APP_DIR = _app_dir()
CONFIG_PATH = APP_DIR / "config.json"
TOKEN_PATH = APP_DIR / "token.json"
REGISTRY_PATH = APP_DIR / "uploads.json"
QUEUE_STATE_PATH = APP_DIR / "queue_state.json"   # pre-multi-queue file; migration only
QUEUES_PATH = APP_DIR / "queues.json"


def queue_state_path(queue_id: str) -> Path:
    return APP_DIR / f"queue_state_{queue_id}.json"


DEFAULTS = {
    "client_secret_path": "",
    "oauth_port": 8710,
    "vod_folder": "",
    "privacy": "private",           # private | unlisted | public
    "category_id": "20",            # 20 = Gaming
    "title_template": "{title} | {streamer} VOD {date}",
    "description_template": "",     # empty = built-in default format
    "notify_subscribers": False,
    "made_for_kids": False,
    "after_upload": "keep",         # keep | trash_video | trash_folder
    "recycle_fallback_delete": False,  # permanently delete if Recycle Bin fails (e.g. network drives)
    # Let LiveStreamDVR delete VODs through its own API, so it stops
    # watching them first (deleting behind its back can crash it)
    "lsdvr_delete": False,
    "lsdvr_url": "",                # e.g. http://192.168.1.10:8080
    "lsdvr_password": "",           # its web panel password; it has no API keys
    "theme": "midnight",            # midnight | dark | light
    "ui_style": "modern",           # modern | classic (fonts/spacing/effects)
    "ui_scale": 1.0,                # 0.75 | 1.0 | 1.25 | 1.5 — multiplier on top of ui_style
    # Automation
    "auto_scan": False,
    "auto_scan_interval_min": 10,
    "auto_queue": True,
    "auto_start": True,
    "auto_only_finalized": True,
    # Playlists
    "playlist_mode": "none",        # none | fixed | template
    "playlist_fixed_id": "",
    "playlist_fixed_title": "",
    "playlist_template": "{streamer} VODs {year}",
    # Rate limiting — retained only as the legacy single-queue state (read by
    # the one-time migration into queues.json) and as the shared project-wide
    # API quota cooldown (quotaExceeded/dailyLimitExceeded/...), which really
    # is global across every queue using the same OAuth client. A channel's
    # own uploadLimitExceeded cooldown lives per-queue instead — see
    # queue_runtime.py.
    "daily_upload_limit": 0,        # 0 = unlimited; stop before YouTube errors
    "cooldown_until": "",           # ISO timestamp while YouTube said "no more"
    "cooldown_reason": "",
    "cooldown_hours": 24.5,         # wait after uploadLimitExceeded before retry
    "quota_cooldown_until": "",     # ISO timestamp; shared API-quota cooldown
    "quota_cooldown_reason": "",
    "upload_speed_limit": 0,        # MB/s cap while uploading; 0 = unlimited
    "verify_uploads": True,         # confirm each upload on YouTube afterwards
    "extra_tags": "",               # always appended to generated tags
    "auto_update_check": True,      # look for new releases on startup
    # Web/Docker mode: OAuth client of type "TVs and Limited Input devices"
    "client_id": "",
    "client_secret": "",
    "active_account": "",           # legacy: channel id of the selected account
    "queue_ui_layout": "switcher",  # switcher | tabs — how multiple queues are shown
}

# Keys copied into every new queue definition (queues.json) from DEFAULTS
# (or from an existing queue, when duplicating one) — everything about how a
# queue scans, builds metadata, and paces its own uploads. Deliberately
# excludes app-wide-only settings (theme, OAuth client, LiveStreamDVR
# connection, auto-update) which stay solely in config.json.
QUEUE_SETTINGS_KEYS = [
    "privacy", "category_id", "title_template", "description_template",
    "notify_subscribers", "made_for_kids", "after_upload",
    "recycle_fallback_delete", "lsdvr_delete",
    "playlist_mode", "playlist_fixed_id", "playlist_fixed_title", "playlist_template",
    "daily_upload_limit", "cooldown_hours", "upload_speed_limit", "verify_uploads",
    "extra_tags",
    "auto_scan", "auto_scan_interval_min", "auto_queue", "auto_start",
    "auto_only_finalized",
]

AFTER_UPLOAD_CHOICES = {
    "Keep local files": "keep",
    "Move video file to Recycle Bin": "trash_video",
    "Move whole VOD folder to Recycle Bin": "trash_folder",
}

CATEGORIES = {
    "Gaming (20)": "20",
    "Entertainment (24)": "24",
    "People & Blogs (22)": "22",
    "Science & Technology (28)": "28",
}


def _ensure_dir() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path: Path, data) -> None:
    """Write via a temp file + os.replace so a crash/power-loss mid-write
    never leaves a truncated/corrupt file behind."""
    _ensure_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    _atomic_write_json(CONFIG_PATH, cfg)


def load_registry() -> dict:
    """Map of VOD folder name -> {video_id, title, uploaded_at}."""
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_registry(reg: dict) -> None:
    _atomic_write_json(REGISTRY_PATH, reg)


def load_queue_state(queue_id: str) -> dict:
    """One queue's pending items + its own channel-limit cooldown:
    {"version": 1, "queue": [...], "metas": {...}, "cooldown_until": "",
    "cooldown_reason": "", "running": False}."""
    try:
        with open(queue_state_path(queue_id), "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and "queue" in data and "metas" in data:
                return data
    except (OSError, ValueError):
        pass
    return {"version": 1, "queue": [], "metas": {},
            "cooldown_until": "", "cooldown_reason": "", "running": False}


def save_queue_state(queue_id: str, data: dict) -> None:
    _atomic_write_json(queue_state_path(queue_id), data)


def load_queues() -> list[dict]:
    """Queue definitions (queues.json): folder + account + per-queue upload
    settings. Migrates the old single-queue config.json/queue_state.json on
    first run after an upgrade (see migrate_legacy_queue)."""
    try:
        with open(QUEUES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("queues"), list):
            return data["queues"]
    except (OSError, ValueError):
        pass
    return []


def save_queues(queues: list[dict]) -> None:
    _atomic_write_json(QUEUES_PATH, {"version": 1, "queues": queues})


def migrate_legacy_queue(cfg: dict) -> list[dict] | None:
    """One-time migration from the pre-multi-queue layout into a single
    synthesized queue. Returns the new queue list and writes queues.json,
    the renamed per-queue state file, and stamped registry entries — or
    None if there was nothing to migrate (queues.json already exists, or
    there's no prior folder configured at all).

    `cfg` (already loaded by the caller) is mutated in place: the legacy
    cooldown is split into either the new queue's own cooldown (channel
    limit) or the shared quota_cooldown_* (project quota), and the old
    flat cooldown_until/cooldown_reason are cleared. The caller is
    responsible for calling save_config(cfg) afterwards.
    """
    if QUEUES_PATH.exists():
        return None
    legacy_folder = (cfg.get("vod_folder") or "").strip()
    if not legacy_folder:
        return None

    import uuid
    queue_id = uuid.uuid4().hex[:12]
    account_id = cfg.get("active_account") or ""
    queue_def = {
        "id": queue_id,
        "name": "Queue 1",
        "vod_folder": legacy_folder,
        "account_id": account_id,
        "enabled": True,
        "order": 0,
    }
    for key in QUEUE_SETTINGS_KEYS:
        queue_def[key] = cfg.get(key, DEFAULTS.get(key))

    try:
        with open(QUEUE_STATE_PATH, "r", encoding="utf-8") as f:
            legacy_state = json.load(f)
    except (OSError, ValueError):
        legacy_state = {"version": 1, "queue": [], "metas": {}}

    reason = cfg.get("cooldown_reason", "")
    until = cfg.get("cooldown_until", "")
    if until and "uploadLimitExceeded" in reason:
        legacy_state["cooldown_until"] = until
        legacy_state["cooldown_reason"] = reason
    elif until:
        cfg["quota_cooldown_until"] = until
        cfg["quota_cooldown_reason"] = reason
    legacy_state.setdefault("running", False)
    save_queue_state(queue_id, legacy_state)

    registry = load_registry()
    if registry:
        changed = False
        for entry in registry.values():
            if "queue_id" not in entry:
                entry["queue_id"] = queue_id
                changed = True
        if changed:
            save_registry(registry)

    cfg["cooldown_until"] = ""
    cfg["cooldown_reason"] = ""

    save_queues([queue_def])
    return [queue_def]
