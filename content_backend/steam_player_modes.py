"""Official Steam player categories, separate from community Store tags."""
from datetime import datetime, timedelta, timezone

BROWSE_SOURCE = "Steam IStoreBrowseService/GetItems supported_player_categoryids"
APPDETAILS_SOURCE = "Steam Store appdetails cc=TW categories"
CATEGORY_FIELDS = ("categories", "categories_source", "categories_checked_at")
PLAYER_CATEGORY_NAMES = {
    1: "Multi-player", 2: "Single-player", 9: "Co-op", 20: "MMO",
    24: "Shared/Split Screen", 27: "Cross-Platform Multiplayer",
    36: "Online PvP", 37: "Shared/Split Screen PvP", 38: "Online Co-op",
    39: "Shared/Split Screen Co-op", 47: "LAN PvP", 48: "LAN Co-op", 49: "PvP",
}


def valid_categories(value: object) -> bool:
    return isinstance(value, list) and all(
        isinstance(row, dict) and type(row.get("id")) is int and row["id"] > 0
        and isinstance(row.get("description"), str)
        for row in value
    )


def has_verified_categories(record: dict) -> bool:
    try:
        value = record.get("categories_checked_at")
        checked = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
        checked_valid = (checked is not None and checked.tzinfo is not None
                         and checked.utcoffset() is not None
                         and checked <= datetime.now(timezone.utc) + timedelta(minutes=5))
    except (ValueError, TypeError, OverflowError):
        checked_valid = False
    return (
        valid_categories(record.get("categories"))
        and record.get("categories_source") in {BROWSE_SOURCE, APPDETAILS_SOURCE}
        and checked_valid
    )


def category_fields(item: dict, details: dict, appid: int, checked_at: str) -> dict:
    """Only the matching official AppID can supply player-mode evidence.

    Browse's player IDs exclude feature/controller IDs. In particular, Remote
    Play Together, achievements and Family Sharing do not prove multiplayer.
    An explicit empty list is checked; missing or malformed data is unknown.
    """
    categories = item.get("categories")
    ids = categories.get("supported_player_categoryids") if isinstance(categories, dict) else None
    if (type(item.get("appid")) is int and item["appid"] == appid
            and item.get("success") == 1 and item.get("visible") is not False
            and isinstance(ids, list) and all(type(x) is int and x > 0 for x in ids)):
        rows = [{"id": category_id,
                 "description": PLAYER_CATEGORY_NAMES.get(category_id, f"Steam category {category_id}")}
                for category_id in dict.fromkeys(ids)]
        return {"categories": rows, "categories_source": BROWSE_SOURCE,
                "categories_checked_at": checked_at}
    if (type(details.get("steam_appid")) is int and details["steam_appid"] == appid
            and details.get("type") == "game" and valid_categories(details.get("categories"))):
        seen = set()
        rows = []
        for row in details["categories"]:
            if row["id"] not in seen:
                rows.append({"id": row["id"], "description": row["description"]})
                seen.add(row["id"])
        return {"categories": rows, "categories_source": APPDETAILS_SOURCE,
                "categories_checked_at": checked_at}
    return {}


def preserve_player_categories(existing: dict, incoming: dict) -> dict:
    """Partial metadata failures cannot erase the last verified player modes."""
    result = dict(incoming)
    if not has_verified_categories(incoming) and has_verified_categories(existing):
        result.update({key: existing[key] for key in CATEGORY_FIELDS})
    return result
