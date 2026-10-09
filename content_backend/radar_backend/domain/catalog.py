"""Eligibility/completeness rules with explicit evidence-validation ports."""

from __future__ import annotations
import hashlib
import json
from typing import Callable
from radar_core.domain.twitch_admission import (
    aware_time,
    is_twitch_qualified,
    normalize_twitch_admission,
)
from radar_backend.domain.content import TAIPEI, valid_date


def metadata_gaps(
    record: dict | None,
    source: dict,
    *,
    preserve_release: Callable,
    verified_categories: Callable,
) -> list[str]:
    if not isinstance(record, dict):
        return ["missing_record"]
    # A fresh date audit can finish while this run still holds an older master.
    source = preserve_release(record, source)
    gaps = []
    if record.get("release_start") != source["release_start"] or int(
        record.get("followers") or 0
    ) != int(source["followers"]):
        gaps.append("source_changed")
    admission = normalize_twitch_admission(
        source.get("twitch_admission"), source.get("appid")
    )
    if (
        admission is not None
        and normalize_twitch_admission(
            record.get("twitch_admission"), source.get("appid")
        )
        != admission
    ):
        gaps.append("twitch_admission")
    if (
        record.get("release_time_utc") is not None
        or record.get("release_timestamp_taipei_date") is not None
        or record.get("release_date_conflict") is True
    ):
        instant = aware_time(record.get("release_time_utc"))
        timestamp_day = (
            instant.astimezone(TAIPEI).date().isoformat()
            if instant is not None
            else None
        )
        conflict = record.get("release_start") != timestamp_day
        if (
            timestamp_day is None
            or record.get("release_timestamp_taipei_date") != timestamp_day
            or type(record.get("release_date_conflict")) is not bool
            or record["release_date_conflict"] != conflict
        ):
            gaps.append("release_date_diagnostics")
    if not (record.get("header_image") or record.get("main_capsule_image")):
        gaps.append("artwork")
    # An empty optional 2x URL is valid when Steam has been checked and has none.
    if not record.get("artwork_checked_at"):
        gaps.append("artwork_quality_unchecked")
    languages = record.get("language_support")
    if not isinstance(languages, dict) or not any(
        isinstance(v, bool) for v in languages.values()
    ):
        gaps.append("languages")
    if (
        not isinstance(record.get("tags"), list)
        or record.get("tags_fetch_status") == "retry"
    ):
        gaps.append("tags")
    elif not record["tags"] and record.get("tags_fetch_status") != "ok":
        gaps.append("tags_unchecked")
    if record.get("tag_labels_language") != "zh-TW":
        gaps.append("tags_zh_tw")
    if (
        record.get("genre_labels_language") != "zh-TW"
        or record.get("genres_fetch_status") == "retry"
    ):
        gaps.append("genres_zh_tw")
    if not record.get("description_checked_at"):
        gaps.append("description_unchecked")
    if not verified_categories(record):
        gaps.append("categories")
    return gaps


def qualified_source(
    source: dict,
    appid: int,
    followers: int,
    day,
    *,
    historical: bool,
    blocked: set[int],
) -> bool:
    return (
        appid > 0
        and (followers >= 5000 or is_twitch_qualified(source))
        and valid_date(day)
        and appid not in blocked
        and (source.get("release_display_precision") == "date_full" or historical)
    )


def enrichment_signature(appid: int, source: dict) -> str:
    signature = f"{appid}:{source['followers']}:{source['release_start']}"
    admission = normalize_twitch_admission(source.get("twitch_admission"), appid)
    if admission is not None:
        signature += (
            ":"
            + hashlib.sha256(
                json.dumps(admission, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()[:20]
        )
    return signature
