"""Runtime clock wiring for official Steam player-category evidence."""

from datetime import datetime, timedelta, timezone
from radar_backend.domain import player_categories as _rules
from radar_backend.domain.player_categories import (
    APPDETAILS_SOURCE,
    BROWSE_SOURCE,
    CATEGORY_FIELDS,
    PLAYER_CATEGORY_NAMES,
)


def valid_categories(value: object) -> bool:
    return _rules.valid_categories(value)


def _category_checked_at(record: dict) -> datetime | None:
    return _rules._category_checked_at(
        record,
        parse_time=lambda value: datetime.fromisoformat(value),
        now=lambda: datetime.now(timezone.utc),
    )


def has_verified_categories(record: dict) -> bool:
    return _rules.has_verified_categories(
        record,
        validate_categories=valid_categories,
        category_checked_at=_category_checked_at,
        sources=(BROWSE_SOURCE, APPDETAILS_SOURCE),
    )


def category_fields(item: dict, details: dict, appid: int, checked_at: str) -> dict:
    return _rules.category_fields(
        item,
        details,
        appid,
        checked_at,
        validate_categories=valid_categories,
        browse_source=BROWSE_SOURCE,
        appdetails_source=APPDETAILS_SOURCE,
        category_names=PLAYER_CATEGORY_NAMES,
    )


def preserve_player_categories(existing: dict, incoming: dict) -> dict:
    return _rules.preserve_player_categories(
        existing,
        incoming,
        verified_categories=has_verified_categories,
        category_checked_at=_category_checked_at,
        category_fields=CATEGORY_FIELDS,
    )
