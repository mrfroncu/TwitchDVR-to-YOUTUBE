"""Upload rate limiting: rolling-window counting and cooldown persistence.

YouTube enforces two separate ceilings:
- the API quota (10 000 units/day, resets at midnight Pacific) -> 403 quotaExceeded
- the channel's own upload limit (rolling ~24 h window)        -> 400 uploadLimitExceeded
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# A little over 24h so we re-try safely after the rolling window has moved on
WINDOW = timedelta(hours=24, minutes=30)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def recent_upload_times(registry: dict) -> list[datetime]:
    """Timestamps of successful uploads inside the rolling window, oldest first."""
    now = datetime.now(timezone.utc)
    times = []
    for entry in registry.values():
        if entry.get("failed"):
            continue
        dt = _parse(entry.get("uploaded_at"))
        if dt and now - dt < WINDOW:
            times.append(dt)
    return sorted(times)


def count_recent(registry: dict) -> int:
    return len(recent_upload_times(registry))


def next_slot(registry: dict, limit: int) -> datetime:
    """When the next upload becomes allowed under a daily limit."""
    times = recent_upload_times(registry)
    if limit <= 0 or len(times) < limit:
        return datetime.now(timezone.utc)
    return times[-limit] + WINDOW


def quota_cooldown(reason: str, hours: float | None = None) -> datetime:
    """How long to wait after YouTube said no. `hours` overrides the wait
    for the channel upload limit (uploadLimitExceeded)."""
    now = datetime.now(timezone.utc)
    if "uploadLimitExceeded" in reason:
        try:
            wait = timedelta(hours=float(hours)) if hours else WINDOW
        except (TypeError, ValueError):
            wait = WINDOW
        return now + wait            # channel limit: rolling ~24 h by default
    # API quota: resets at midnight Pacific time
    try:
        from zoneinfo import ZoneInfo
        pacific = datetime.now(ZoneInfo("America/Los_Angeles"))
        nxt = (pacific + timedelta(days=1)).replace(hour=0, minute=10,
                                                    second=0, microsecond=0)
        return nxt.astimezone(timezone.utc)
    except Exception:                # tz database unavailable
        return now + timedelta(hours=8)


def get_cooldown(cfg: dict) -> datetime | None:
    """Active cooldown end time, or None (expired cooldowns read as None)."""
    dt = _parse(cfg.get("cooldown_until"))
    if dt and dt > datetime.now(timezone.utc):
        return dt
    return None


def set_cooldown(cfg: dict, until: datetime | None, reason: str = "") -> None:
    cfg["cooldown_until"] = until.isoformat() if until else ""
    cfg["cooldown_reason"] = reason


def fmt_local(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")
