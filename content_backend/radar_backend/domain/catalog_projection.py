"""Browser fields and revisions derived only from accepted AppID records."""

import hashlib
import json

FIELDS = (
    "appid",
    "name",
    "name_en",
    "display_name",
    "name_zh_tw",
    "name_zh_cn",
    "name_zh_tw_traditional",
    "name_zh_cn_traditional",
    "name_en_traditional",
    "language_support",
    "release_start",
    "release_end",
    "release_precision",
    "release_display_precision",
    "release_date_timezone",
    "followers",
    "follower_checked_at",
    "recent_source",
    "first_week_qualified_at",
    "header_image",
    "header_image_2x",
    "main_capsule_image",
    "main_capsule_image_2x",
    "tags",
    "genres",
    "tag_labels_zh_tw",
    "genre_labels_zh_tw",
    "twitch_admission",
    "steam_type",
    "sexual_content_screened",
    "release_time_utc",
    "release_date_conflict",
    "release_timestamp_taipei_date",
    "release_store_date",
    "release_date_normalization",
    "release_display_provider",
    "release_date_verified_at",
    "categories",
    "categories_source",
    "categories_checked_at",
)

# List pages never read these; a game page reads them from games/<appid>.json.
# catalog.json carried them until 2026-11. A file written before then is still
# an exact projection of the accepted records, so it stays valid until the
# next write replaces it.
RETIRED_FIELDS = (
    "small_capsule_image",
    "capsule_image",
    "tag_ids",
    "content_enriched_at",
    "tags_fetch_status",
    "official_ge5000",
    "content_descriptorids",
    "short_description",
    "short_description_en",
    "short_description_language",
    "short_description_source",
    "description_source_url",
)


def catalog_revision(rows: list[dict], *, json_codec=json, hash_codec=hashlib) -> str:
    """Metadata changes in any accepted field invalidate the release snapshot."""
    canonical = json_codec.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hash_codec.sha256(canonical.encode()).hexdigest()[:20]


def catalog_payload(
    rows: list[dict], generated_at: str, revision: str, *, fields=FIELDS
) -> dict:
    return {
        "version": 3,
        "revision": revision,
        "generated_at": generated_at,
        "count": len(rows),
        "games": [{key: row[key] for key in fields if key in row} for row in rows],
    }


def _same_json_value(actual, expected) -> bool:
    """Compare JSON values without treating booleans as integer evidence."""
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            _same_json_value(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _same_json_value(left, right) for left, right in zip(actual, expected)
        )
    return actual == expected or (
        isinstance(expected, float) and actual != actual and expected != expected
    )


def catalog_matches(actual: dict, expected: dict) -> bool:
    """Match the browser contract; an old generated_at is valid for a no-op."""
    return (
        type(actual.get("version")) is int
        and actual["version"] == expected["version"]
        and type(actual.get("count")) is int
        and actual["count"] == expected["count"]
        and type(actual.get("revision")) is str
        and actual["revision"] == expected["revision"]
        and _same_json_value(actual.get("games"), expected["games"])
    )
