"""Preserve verified release and admission evidence without storage access."""

from datetime import datetime, timezone
from radar_core.domain.twitch_admission import aware_time, preserve_twitch_admission, taipei_release_day
from typing import Callable

from radar_backend.domain.content import TAIPEI

RELEASE_FIELDS = (
    "release_raw",
    "release_start",
    "release_end",
    "release_precision",
    "release_display_precision",
    "release_display_provider",
    "release_date_timezone",
    "release_date_basis",
    "release_date_verified_at",
    "release_time_utc",
    "release_time_source",
    "release_timestamp_taipei_date",
    "release_date_conflict",
    "post_followers_store_verified",
    "post_followers_store_verified_at",
    "release_store_date",
    "release_date_normalization",
)


def keep_newer_release(
    existing: dict, incoming: dict, *, preserve_categories: Callable
) -> dict:
    """Content events and master snapshots cannot roll back a newer date audit."""

    def checked_at(row):
        try:
            value = datetime.fromisoformat(
                str(row.get("release_date_verified_at")).replace("Z", "+00:00")
            )
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    result = preserve_categories(
        existing, preserve_twitch_admission(existing, incoming)
    )
    # Admission is an independent source. Ordinary metadata refreshes cannot
    # erase a verified Twitch discovery, including when Followers remain low.
    if existing.get("release_display_precision") == "date_full" and checked_at(
        existing
    ) > checked_at(incoming):
        for key in RELEASE_FIELDS:
            if key in existing:
                result[key] = existing[key]
            else:
                result.pop(key, None)
        backfill_release_diagnostics(result)
        # A kept Store-day admission one day off its release time takes
        # Taiwan's day, as the Steam master's rows do (core #70). The date
        # confirmation time stays as it was.
        result = taipei_release_day(result)
    return result


def backfill_release_diagnostics(row: dict) -> None:
    """Derive the timestamp diagnostics that records verified before they existed lack.

    The kept evidence wins over a fresh historical record, which used to drop
    the fresh diagnostics as well and left the record incomplete for good.
    Both values follow from the kept instant and date alone; a record holding
    either one is left untouched.
    """
    if "release_timestamp_taipei_date" in row or "release_date_conflict" in row:
        return
    instant = aware_time(row.get("release_time_utc"))
    if instant is None:
        return
    day = instant.astimezone(TAIPEI).date().isoformat()
    row["release_timestamp_taipei_date"] = day
    row["release_date_conflict"] = row.get("release_start") != day
