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
    "small_capsule_image",
    "capsule_image",
    "tags",
    "genres",
    "tag_ids",
    "tag_labels_zh_tw",
    "genre_labels_zh_tw",
    "content_enriched_at",
    "tags_fetch_status",
    "twitch_admission",
    "steam_type",
    "sexual_content_screened",
    "release_time_utc",
    "release_date_conflict",
    "official_ge5000",
    "release_timestamp_taipei_date",
    "content_descriptorids",
    "release_store_date",
    "release_date_normalization",
    "release_display_provider",
    "release_date_verified_at",
    "categories",
    "categories_source",
    "categories_checked_at",
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
