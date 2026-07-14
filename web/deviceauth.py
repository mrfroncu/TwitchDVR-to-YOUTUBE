"""Google OAuth 2.0 device flow — sign-in for headless/Docker deployments.

The UI shows a short code; the user enters it at google.com/device on any
device. Requires an OAuth client of type "TVs and Limited Input devices"
(YouTube scopes are on the device flow's allow-list).
"""
from __future__ import annotations

import json
import time

import requests
from google.oauth2.credentials import Credentials

from app import config
from app.auth import SCOPES, _save

DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"


def read_client_from_config(cfg: dict) -> tuple[str, str]:
    """Client id/secret from settings, else from a client_secret*.json
    dropped into the config volume."""
    client_id = (cfg.get("client_id") or "").strip()
    client_secret = (cfg.get("client_secret") or "").strip()
    if client_id and client_secret:
        return client_id, client_secret
    for path in sorted(config.APP_DIR.glob("client_secret*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        section = data.get("installed") or data.get("web") or {}
        if section.get("client_id") and section.get("client_secret"):
            return section["client_id"], section["client_secret"]
    raise RuntimeError(
        "No OAuth client configured. Paste the client ID and secret in "
        "Settings (create a 'TVs and Limited Input devices' OAuth client in "
        "Google Cloud Console), or drop its client_secret.json into the "
        "config directory.")


WRONG_CLIENT_TYPE_MSG = (
    "This OAuth client is the wrong type for the web version. The device "
    "sign-in requires a client of type “TVs and Limited Input devices” — a "
    "“Desktop app” client (the one the desktop version uses) will not work "
    "here. In Google Cloud Console open APIs & Services → Credentials → "
    "Create credentials → OAuth client ID → choose “TVs and Limited Input "
    "devices”, then upload/paste that client here.")


def start(client_id: str) -> dict:
    """Begin the flow: returns user_code / verification_url / device_code."""
    resp = requests.post(DEVICE_CODE_URL, data={
        "client_id": client_id,
        "scope": " ".join(SCOPES),
    }, timeout=30)
    if resp.status_code != 200:
        text = resp.text[:400]
        if "invalid_client" in text or "Invalid client type" in text:
            raise RuntimeError(WRONG_CLIENT_TYPE_MSG)
        raise RuntimeError(f"device code request failed: {text}")
    return resp.json()


def poll_once(client_id: str, client_secret: str, device_code: str):
    """One token poll. Returns Credentials when granted, None while pending,
    raises on denial/expiry."""
    resp = requests.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "device_code": device_code,
        "grant_type": GRANT_TYPE,
    }, timeout=30)
    data = resp.json()
    if resp.status_code == 200:
        creds = Credentials(
            token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            token_uri=TOKEN_URL,
            client_id=client_id,
            client_secret=client_secret,
            scopes=SCOPES,
        )
        _save(creds)
        return creds
    error = data.get("error", "")
    if error in ("authorization_pending", "slow_down"):
        if error == "slow_down":
            time.sleep(5)
        return None
    if error == "access_denied":
        raise RuntimeError("sign-in was denied in the Google prompt")
    if error == "expired_token":
        raise RuntimeError("the code expired — start the sign-in again")
    raise RuntimeError(f"token poll failed: {resp.text[:300]}")
