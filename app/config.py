"""Configuration and persistent state, stored in %APPDATA%\\TwitchDVR-to-YouTube."""
import json
import os
from pathlib import Path

APP_NAME = "TwitchDVR-to-YouTube"

APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
CONFIG_PATH = APP_DIR / "config.json"
TOKEN_PATH = APP_DIR / "token.json"
REGISTRY_PATH = APP_DIR / "uploads.json"

DEFAULTS = {
    "client_secret_path": "",
    "oauth_port": 8710,
    "vod_folder": "",
    "privacy": "private",           # private | unlisted | public
    "category_id": "20",            # 20 = Gaming
    "title_template": "{title} | {streamer} VOD {date}",
    "notify_subscribers": False,
    "made_for_kids": False,
    "chunk_mb": 8,
}

CATEGORIES = {
    "Gaming (20)": "20",
    "Entertainment (24)": "24",
    "People & Blogs (22)": "22",
    "Science & Technology (28)": "28",
}


def _ensure_dir() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    _ensure_dir()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def load_registry() -> dict:
    """Map of VOD folder name -> {video_id, title, uploaded_at}."""
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_registry(reg: dict) -> None:
    _ensure_dir()
    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(reg, f, indent=2)
