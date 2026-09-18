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


def _pacific_midnight_today() -> datetime | None:
    """Start of the current day in US Pacific time (tz-aware), or None if
    the system's tz database is unavailable. Shared so the reactive cooldown
    below and the proactive quota panel agree on the same reset boundary."""
    try:
        from zoneinfo import ZoneInfo
        pacific = datetime.now(ZoneInfo("America/Los_Angeles"))
        return pacific.replace(hour=0, minute=0, second=0, microsecond=0)
    except Exception:
        return None


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
    midnight = _pacific_midnight_today()
    if midnight is None:             # tz database unavailable
        return now + timedelta(hours=8)
    return (midnight + timedelta(days=1, minutes=10)).astimezone(timezone.utc)


QUOTA_DAILY_UNITS = 10000
QUOTA_UNIT_COST = 1600


def units_used_today(registry: dict, unit_cost: int = QUOTA_UNIT_COST) -> int:
    """API units spent since the last Pacific midnight, counted from
    successful uploads already recorded in the registry (uploads.json) —
    reuses that data instead of tracking a separate running counter."""
    midnight = _pacific_midnight_today()
    if midnight is None:
        return 0
    boundary = midnight.astimezone(timezone.utc)
    count = sum(1 for entry in registry.values()
               if not entry.get("failed")
               and (dt := _parse(entry.get("uploaded_at"))) is not None
               and dt >= boundary)
    return count * unit_cost


def uploads_remaining_today(registry: dict, daily_quota: int = QUOTA_DAILY_UNITS,
                            unit_cost: int = QUOTA_UNIT_COST) -> int:
    used = units_used_today(registry, unit_cost)
    return max(0, (daily_quota - used) // unit_cost)


def eta_for_queue(pending_count: int, remaining_today: int,
                  daily_quota: int = QUOTA_DAILY_UNITS,
                  unit_cost: int = QUOTA_UNIT_COST) -> str:
    """Rough finish estimate for `pending_count` queued uploads at the
    current daily quota pace (~6 uploads/day by default)."""
    if pending_count <= 0:
        return "done"
    if pending_count <= remaining_today:
        return "today"
    per_day = max(1, daily_quota // unit_cost)
    extra = pending_count - remaining_today
    # `extra` is what's left once today's remaining slots are used up, so the
    # number of *additional* days is just the ceiling — no separate "+1 for
    # today" term (today already contributed `remaining_today`, possibly 0).
    days = -(-extra // per_day)
    return f"~{days} day(s)"


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
