"""Taiwan Steam price, CSRR rating and Steam Deck status for every catalog game.

One small file, data/store_facts.json, is checked on every media run (four
times a day) for all games, unlike the weekly media files: prices change with
sales. Each game keeps the time Steam last confirmed its values, so the page
can say how fresh a price is.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

FACTS_FILE = "store_facts.json"
SCHEMA_VERSION = 1
# Publish an unchanged file at least this often so its check times stay fresh.
REPUBLISH_INTERVAL = timedelta(hours=20)
CSRR_AGENCY = 13
CSRR_LABELS = {"G": "普遍級", "P6": "保護級", "C12": "輔12級", "C15": "輔15級", "R": "限制級"}
DECK = {1: "unsupported", 2: "playable", 3: "verified"}
# Taiwan's descriptor names, in the order the rating rules list them. Steam
# returns them in Chinese or English, sometimes split mid-phrase or joined.
DESCRIPTORS = (
    ("性", ("性", "sex", "sexuality", "sexual")),
    ("暴力", ("暴力", "violence", "violent")),
    ("恐怖", ("恐怖", "terror", "fear", "horror")),
    ("毒品", ("毒品", "drug", "drugs")),
    ("不當言語", ("不當言語", "inappropriate language", "inappropriate", "language")),
    ("反社會性", ("反社會性", "anti-social", "antisocial")),
    ("菸酒", ("菸酒", "tobacco", "alcohol", "tobacco and alcohol")),
    ("牌類及益智娛樂", ("牌類及益智娛樂",)),
    ("戀愛交友", ("戀愛交友",)),
)
_SPLIT = re.compile(r"[,，、;；/]+|\band\b", re.IGNORECASE)


def aware_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _cents(value: object) -> int | None:
    text = str(value) if type(value) in (int, str) else ""
    return int(text) if text.isascii() and text.isdigit() else None


def descriptors(rows: object) -> list[str]:
    tokens = set()
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, str):
            tokens.update(part.strip().lower() for part in _SPLIT.split(row) if part.strip())
    return [label for label, names in DESCRIPTORS if tokens & set(names)]


def rating(item: dict) -> dict | None:
    """Only Taiwan's CSRR rating; other agencies and survey notes are left out."""
    row = item.get("game_rating")
    if not isinstance(row, dict) or row.get("agency") != CSRR_AGENCY:
        return None
    code = row.get("rating")
    if code not in CSRR_LABELS:
        return None
    return {"code": code, "label": CSRR_LABELS[code], "descriptors": descriptors(row.get("descriptors"))}


def price(item: dict) -> dict | None:
    if item.get("is_free") is True:
        return {"free": True}
    option = item.get("best_purchase_option")
    if not isinstance(option, dict):
        return None
    final = _cents(option.get("final_price_in_cents"))
    if final is None:
        return None
    result = {"free": False, "final_cents": final}
    original = _cents(option.get("original_price_in_cents"))
    pct = option.get("discount_pct")
    if (original is not None and original > final and type(pct) is int and 0 < pct < 100
            and option.get("hide_discount_pct_for_compliance") is not True):
        result.update(original_cents=original, discount_pct=pct)
        ends = [row.get("discount_end_date") for row in option.get("active_discounts") or []
                if isinstance(row, dict) and type(row.get("discount_end_date")) is int]
        if ends:
            result["discount_ends_at"] = timestamp(datetime.fromtimestamp(min(ends), timezone.utc))
    return result


def steam_deck(item: dict) -> str | None:
    category = (item.get("platforms") or {}).get("steam_deck_compat_category")
    return DECK.get(category) if type(category) is int else None


def store_facts(appid: int, item: dict) -> dict:
    """The page-facing facts from one Store Browse item; no timestamps."""
    if not isinstance(item, dict) or item.get("appid") != appid:
        raise ValueError(f"Steam store item does not belong to AppID {appid}")
    return {"price": price(item), "rating": rating(item), "steam_deck": steam_deck(item)}


FACT_KEYS = ("price", "rating", "steam_deck")


def same_facts(entry: object, facts: dict) -> bool:
    return isinstance(entry, dict) and all(entry.get(key) == facts[key] for key in FACT_KEYS)


def facts_document(games: dict, checked_at: str, source: str) -> dict:
    return {"schema_version": SCHEMA_VERSION, "checked_at": checked_at, "currency": "TWD",
            "country": "TW", "source": source,
            "games": {key: games[key] for key in sorted(games, key=int)}}


def _valid_price(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict) or type(value.get("free")) is not bool:
        return False
    if value["free"]:
        return set(value) == {"free"}
    final = value.get("final_cents")
    if type(final) is not int or final < 0:
        return False
    if "original_cents" in value or "discount_pct" in value:
        original, pct = value.get("original_cents"), value.get("discount_pct")
        if type(original) is not int or original <= final or type(pct) is not int or not 0 < pct < 100:
            return False
    ends = value.get("discount_ends_at")
    return ends is None or aware_time(ends) is not None


def valid_entry(entry: object) -> bool:
    if not isinstance(entry, dict) or aware_time(entry.get("checked_at")) is None:
        return False
    rating_value = entry.get("rating")
    rating_ok = rating_value is None or (
        isinstance(rating_value, dict) and rating_value.get("code") in CSRR_LABELS
        and rating_value.get("label") == CSRR_LABELS[rating_value["code"]]
        and isinstance(rating_value.get("descriptors"), list)
        and all(isinstance(name, str) for name in rating_value["descriptors"]))
    return (_valid_price(entry.get("price")) and rating_ok
            and entry.get("steam_deck") in (None, *DECK.values()))


def valid_document(document: object) -> bool:
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        return False
    games = document.get("games")
    return (aware_time(document.get("checked_at")) is not None and isinstance(games, dict)
            and all(isinstance(key, str) and key.isdigit() for key in games)
            and all(valid_entry(entry) for entry in games.values()))


def needs_publication(previous: dict | None, current: dict, now: datetime) -> bool:
    """Changed values publish at once; unchanged ones at least every 20 hours."""
    if not valid_document(previous):
        return True
    old, new = previous["games"], current["games"]
    if set(old) != set(new) or any(not same_facts(old[key], new[key]) for key in new):
        return True
    published = aware_time(previous.get("checked_at"))
    return published is None or now - published >= REPUBLISH_INTERVAL
