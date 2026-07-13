"""Google OAuth (loopback redirect) and YouTube API service construction.

Sign-in opens the default browser at accounts.google.com with a redirect to
http://127.0.0.1:<port>/ where a temporary local server catches the code.
The Google account chooser shown during sign-in is also where a brand /
secondary YouTube channel is picked.
"""
from __future__ import annotations

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from . import config

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    # Full scope: needed for playlist management (create / add items);
    # it also covers the read-only channel lookup.
    "https://www.googleapis.com/auth/youtube",
]

SUCCESS_MESSAGE = (
    "TwitchDVR-to-YouTube is now connected. You can close this tab "
    "and return to the app."
)


def load_credentials() -> Credentials | None:
    """Returns saved credentials, refreshed if needed, else None."""
    if not config.TOKEN_PATH.exists():
        return None
    try:
        import json
        stored = set(json.loads(
            config.TOKEN_PATH.read_text(encoding="utf-8")).get("scopes") or [])
        if not set(SCOPES) <= stored:
            # Token predates a scope addition (e.g. playlists) — force re-consent
            return None
    except (ValueError, OSError):
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(config.TOKEN_PATH), SCOPES)
    except (ValueError, OSError):
        return None
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save(creds)
            return creds
        except Exception:
            return None
    return None


def sign_in(client_secret_path: str, port: int) -> Credentials:
    """Runs the browser OAuth flow. Blocking — call from a worker thread."""
    flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)
    creds = flow.run_local_server(
        host="127.0.0.1",
        port=port,
        prompt="select_account consent",
        authorization_prompt_message="",
        success_message=SUCCESS_MESSAGE,
        open_browser=True,
    )
    _save(creds)
    return creds


def sign_out() -> None:
    try:
        config.TOKEN_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def _save(creds: Credentials) -> None:
    config.APP_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.TOKEN_PATH, "w", encoding="utf-8") as f:
        f.write(creds.to_json())


def build_service(creds: Credentials):
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def fetch_channel(service) -> dict | None:
    """Channel the credentials act on: {'id': ..., 'title': ...} or None."""
    resp = service.channels().list(part="snippet", mine=True).execute()
    items = resp.get("items") or []
    if not items:
        return None
    ch = items[0]
    return {"id": ch["id"], "title": ch["snippet"]["title"]}


def describe_api_error(exc: Exception) -> str:
    """Turn API errors into an actionable message where possible."""
    text = str(exc)
    if "accessNotConfigured" in text or "has not been used in project" in text:
        import re
        m = re.search(r"project (\d+)", text)
        url = ("https://console.developers.google.com/apis/api/"
               "youtube.googleapis.com/overview")
        if m:
            url += f"?project={m.group(1)}"
        return ("YouTube Data API v3 is not enabled for your Google Cloud "
                f"project. Enable it here:\n{url}\n"
                "then wait a few minutes and try again.")
    if "invalid_grant" in text:
        return ("Saved session is no longer valid (testing-mode tokens expire "
                "after ~7 days). Sign in again.")
    return text
