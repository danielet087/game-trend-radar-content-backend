"""Enrichment use case: collect sources, validate evidence, transform one record."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable
from radar_backend.domain.player_categories import category_fields
from radar_backend.domain.content import (
    ContentSnapshot, record_from_snapshot, taxonomy_fields,
    validate_enrichment_input, validate_store_snapshot,
)

@dataclass(frozen=True)
class EnrichmentPorts:
    browse: Callable
    details: Callable
    taxonomy: Callable
    describe: Callable
    convert: Callable
    sleep: Callable
    utc_now: Callable
    now: Callable


def build_record(session, *, ports: EnrichmentPorts, appid: int, followers: int | None,
                 event_release_date: str, follower_checked_at: str | None,
                 allow_historical: bool = False, twitch_admission: dict | None = None,
                 follower_status: str | None = None, follower_unavailable_at: str | None = None) -> dict:
    admission = validate_enrichment_input(appid, followers, twitch_admission,
                                          follower_checked_at=follower_checked_at,
                                          follower_status=follower_status,
                                          follower_unavailable_at=follower_unavailable_at)
    en = ports.browse(session, appid, "english")
    ports.sleep(1.0)
    tw = ports.browse(session, appid, "tchinese")
    ports.sleep(1.0)
    cn = ports.browse(session, appid, "schinese")
    details = ports.details(session, appid)
    # Reject adult/unavailable/mismatched identities before another source query.
    validate_store_snapshot(appid, en, details, admission)
    taxonomy = taxonomy_fields(ports.taxonomy(session, appid), appid, ports.utc_now())
    if not taxonomy["tags"] and en.get("tagids"):
        taxonomy["tags_fetch_status"] = "retry"
    record = record_from_snapshot(
        ContentSnapshot(en, tw, cn, details), appid=appid, followers=followers,
        event_release_date=event_release_date, follower_checked_at=follower_checked_at,
        allow_historical=allow_historical, admission=admission, taxonomy=taxonomy,
        descriptions={}, checked_at=ports.utc_now(), observed_now=ports.now(),
        convert=ports.convert,
        follower_status=follower_status, follower_unavailable_at=follower_unavailable_at,
    )
    # Resolve translation evidence only after exact-date/artwork qualification.
    # The legacy translation helper reads a curated JSON; it is an injected port,
    # so the domain snapshot transformation itself never loads that file.
    descriptions = ports.describe(
        appid, (en.get("basic_info") or {}).get("short_description"),
        (tw.get("basic_info") or {}).get("short_description") or details.get("short_description"),
        (cn.get("basic_info") or {}).get("short_description"),
    )
    result = {}
    for key, value in record.items():
        if key == "description_checked_at":
            result.update(descriptions)
        result[key] = value
    return result


def refresh_player_categories(session, appid: int, *, browse, details, utc_now) -> dict:
    """Bounded category-only refresh; no date or Followers collection."""
    item = browse(session, appid, "english")
    fields = category_fields(item, {}, appid, utc_now())
    if not fields:
        fields = category_fields({}, details(session, appid), appid, utc_now())
    if not fields:
        raise RuntimeError(f"Steam player categories unavailable for AppID {appid}")
    return fields
