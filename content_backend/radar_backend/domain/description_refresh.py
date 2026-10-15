"""Independent, date-based description refresh deadlines (no metadata gaps)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

OFFICIAL_CHINESE_SOURCES = frozenset({"steam_tchinese", "steam_schinese_converted"})
RETRY_DELAY = timedelta(hours=6)
MINIMUM_TIME = datetime.min.replace(tzinfo=timezone.utc)
TAIPEI = ZoneInfo("Asia/Taipei")


def aware_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def refresh_interval(record: dict, now: datetime) -> timedelta:
    try:
        release = date.fromisoformat(str(record.get("release_start")))
    except (ValueError, TypeError):
        return timedelta(days=7)
    days = (release - now.astimezone(TAIPEI).date()).days
    if days < -30:
        return timedelta(days=30)
    if record.get("short_description_source") in OFFICIAL_CHINESE_SOURCES:
        return timedelta(days=7)
    return timedelta(days=1 if -30 <= days <= 60 else 7)


def next_check(record: dict, checked: datetime) -> datetime:
    deadline = checked + refresh_interval(record, checked)
    # Do not delay the first daily check past the start of the release window.
    if record.get("short_description_source") not in OFFICIAL_CHINESE_SOURCES:
        try:
            release = date.fromisoformat(str(record.get("release_start")))
            daily_window = datetime.combine(release - timedelta(days=60), datetime.min.time(), tzinfo=TAIPEI)
            if checked < daily_window < deadline:
                deadline = daily_window
        except (ValueError, TypeError):
            pass
    return deadline.astimezone(timezone.utc)


def due_at(record: dict, checkpoint: dict, now: datetime) -> datetime:
    checked = aware_time(record.get("description_checked_at"))
    attempted = aware_time(checkpoint.get("attempted_at"))
    # A newer successful event supersedes an older failed refresh checkpoint.
    if checkpoint.get("outcome") == "error" and attempted and (not checked or attempted >= checked):
        return aware_time(checkpoint.get("retry_at")) or attempted + RETRY_DELAY
    if checked is None:
        return MINIMUM_TIME
    # Recompute if a game's release date or official-language source changed.
    return next_check(record, checked)


def select_due(records: list[dict], checkpoints: dict, now: datetime, limit: int) -> list[dict]:
    rows = [(due_at(row, checkpoints.get(str(row["appid"]), {}), now), int(row["appid"]), row)
            for row in records]
    return [row for deadline, _, row in sorted(rows, key=lambda item: item[:2]) if deadline <= now][:limit]


def retry_pending(records: list[dict], checkpoints: dict) -> list[int]:
    return [int(row["appid"]) for row in records
            if checkpoints.get(str(row["appid"]), {}).get("outcome") == "error"
            and (aware_time(checkpoints[str(row["appid"])].get("attempted_at")) or MINIMUM_TIME)
            >= (aware_time(row.get("description_checked_at")) or MINIMUM_TIME)]
