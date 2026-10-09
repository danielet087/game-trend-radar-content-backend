"""Runtime category-evidence wiring for release and completeness rules."""

from radar_backend.adapters.player_categories import (
    has_verified_categories,
    preserve_player_categories,
)
from radar_backend.domain.catalog import metadata_gaps as _metadata_gaps
from radar_backend.domain.release import keep_newer_release as _keep_newer_release


def keep_newer_release(existing: dict, incoming: dict) -> dict:
    return _keep_newer_release(
        existing,
        incoming,
        preserve_categories=preserve_player_categories,
    )


def metadata_gaps(record: dict | None, source: dict) -> list[str]:
    return _metadata_gaps(
        record,
        source,
        preserve_release=keep_newer_release,
        verified_categories=has_verified_categories,
    )
