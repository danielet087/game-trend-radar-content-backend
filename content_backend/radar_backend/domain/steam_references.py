"""Steam reference rows: the Steam version of a public console game.

A console game (data/nintendo_upcoming.json) whose IGDB record names exactly
one Steam product gets that product's public Steam facts here, so its game
page can be built on the Steam version. A reference row is not a Steam radar
admission: no Followers, no Steam calendar entry and no radar activity. It
only exists while the console game is public, still names this product, the
product is not already in the Steam catalog, every console game naming it has
the same title, and the product passes the same sexual-content screen.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import unicodedata

from radar_backend.domain.content import (STORE_PAGE, TAIPEI, exact_store_display_date, official_chinese_title,
                                          read_support, steam_asset_url)
from radar_backend.domain.player_categories import category_fields

REFERENCES_FILE = "steam_references.json"
STATUS_FILE = "steam_reference_status.json"
CONSOLE_FILE = "nintendo_upcoming.json"
SCHEMA_VERSION = 1
METHOD = "igdb_console_steam_link_v1"
REFRESH_INTERVAL = timedelta(days=3)
RETRY_DELAY = timedelta(hours=6)
MINIMUM_TIME = datetime.min.replace(tzinfo=timezone.utc)

# Same rule as the site's nintendoSteamIdentity: an https Store product path.
_STEAM_URL = re.compile(r"https://store\.steampowered\.com/app/([1-9][0-9]{0,9})(?:/[^/?#]*)?/?(?:[?#].*)?\Z")
_MARKS = re.compile(r"[™®©℗℠]")
_WORDS = re.compile(r"[0-9a-z]+|[぀-ヿ㐀-鿿]")
_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}
# A Store title's subtitle or edition starts at one of these.
_SUBTITLE = re.compile(r":|：|\s[-–—]|\s/|\s\+\s|\s\(|（")


def aware_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def steam_link(game: dict) -> int | None:
    """The one Steam product a public console record names; None if none or ambiguous."""
    ids = set()
    for row in game.get("websites") or []:
        match = _STEAM_URL.fullmatch(row.get("url") or "") if isinstance(row, dict) and isinstance(row.get("url"), str) else None
        if match:
            ids.add(int(match[1]))
    if len(ids) != 1:
        return None
    appid = next(iter(ids))
    declared = game.get("steam_appid")
    if declared is not None and (type(declared) is not int or declared != appid):
        return None
    return appid


def console_links(native: object) -> dict[int, list[dict]]:
    """AppID -> the public console games naming it (IGDB id and English title)."""
    if not isinstance(native, dict) or native.get("schema_version") != 1 or not isinstance(native.get("games"), list):
        raise ValueError("Malformed console catalog")
    links: dict[int, list[dict]] = {}
    for game in native["games"]:
        if not isinstance(game, dict) or type(game.get("igdb_id")) is not int or game["igdb_id"] <= 0:
            continue
        appid = steam_link(game)
        name = game.get("name_en") or game.get("name") or ""
        if appid is None or not isinstance(name, str) or not name.strip():
            continue
        links.setdefault(appid, []).append({"igdb_id": game["igdb_id"], "name_en": name.strip()})
    return {appid: sorted(rows, key=lambda row: row["igdb_id"]) for appid, rows in sorted(links.items())}


def title_tokens(value: object) -> tuple[str, ...]:
    text = unicodedata.normalize("NFKC", _MARKS.sub("", value if isinstance(value, str) else "")).casefold()
    return tuple(_ROMAN.get(word, word) for word in _WORDS.findall(text.replace("&", " and ")))


def _numeral(token: str) -> bool:
    return token.isdigit()


def title_match(console_name: object, steam_name: object) -> str | None:
    """How a Store title names the console game, or None when it names something else.

    The Store title may add a subtitle or an edition after a separator
    ("+ Re Mind (DLC)", ": Definitive Edition") or a brand around the same
    words ("Disney Epic Mickey: Rebrushed"), but never a number: "Hela 2" is
    another game. The console title may not drop words, so a collection such
    as "KINGDOM HEARTS -HD 1.5+2.5 ReMIX-" never matches one game inside it.
    """
    wanted = title_tokens(console_name)
    full = title_tokens(steam_name)
    if not wanted or not full:
        return None
    if wanted == full:
        return "exact"
    head = _SUBTITLE.split(steam_name, maxsplit=1)[0] if isinstance(steam_name, str) else ""
    if title_tokens(head) == wanted:
        return "subtitle"
    if len(wanted) >= 2 and len(full) > len(wanted):
        extra = (full[len(wanted):] if full[:len(wanted)] == wanted
                 else full[:-len(wanted)] if full[-len(wanted):] == wanted else None)
        if extra and not any(_numeral(token) for token in extra):
            return "contained"
    return None


def reference_release(item: dict, details: dict, now: datetime) -> dict:
    """The Steam version's day as the Taiwan Store shows it; None when not exact."""
    release = item.get("release") if isinstance(item.get("release"), dict) else {}
    stamp = release.get("steam_release_date")
    instant = (datetime.fromtimestamp(int(stamp), tz=timezone.utc)
               if type(stamp) is int or (isinstance(stamp, str) and stamp.isdigit()) else None)
    visible = details.get("release_date") if isinstance(details.get("release_date"), dict) else {}
    official_day = exact_store_display_date(visible.get("date"))
    coming = visible.get("coming_soon") if type(visible.get("coming_soon")) is bool else release.get("is_coming_soon")
    released = bool(coming is False and (instant is None or instant <= now) and (official_day or instant))
    exact = released or release.get("coming_soon_display") == "date_full"
    day, basis = None, None
    if exact and official_day:
        day, basis = official_day, "steam_taiwan_store_date"
    elif exact and instant is not None:
        day, basis = instant.astimezone(TAIPEI).date().isoformat(), "steam_release_timestamp"
    return {"release_start": day, "release_precision": "day" if day else None,
            "release_coming_soon": not released, "release_date_basis": basis,
            "release_time_utc": timestamp(instant) if instant else None}


# The page needs these taxonomy fields; sources and fetch statuses stay in the job status.
TAXONOMY_KEYS = ("tags", "tag_labels_zh_tw", "genres", "genre_labels_zh_tw")


def reference_record(appid: int, links: list[dict], *, english: dict, traditional: dict, simplified: dict,
                     details: dict, taxonomy: dict, descriptions: dict, match: str,
                     checked_at: str, now: datetime, convert) -> dict:
    """The public Steam row for a reference; raises ValueError for an unusable product.

    Only what the game page reads is kept: this file is read with the catalog.
    """
    for item in (english, traditional, simplified):
        if not isinstance(item, dict) or item.get("appid") != appid or item.get("success", 1) != 1 or item.get("visible") is False:
            raise ValueError(f"Steam AppID {appid} is unavailable in the Taiwan Store")
    if details.get("type") != "game" or details.get("steam_appid") != appid:
        raise ValueError(f"Steam AppID {appid} is not a verified Steam game")
    name_en = str(english.get("name") or details.get("name") or "").strip()
    if not name_en:
        raise ValueError(f"Steam AppID {appid} has no Store title")
    name_tw = official_chinese_title(traditional.get("name"))
    name_cn = official_chinese_title(simplified.get("name"))
    name_tw_traditional = convert(name_tw) if name_tw else None
    name_cn_traditional = convert(name_cn) if name_cn else None
    assets = english.get("assets") if isinstance(english.get("assets"), dict) else {}
    fmt = assets.get("asset_url_format")
    header = steam_asset_url(appid, fmt, assets.get("header")) or ""
    main = steam_asset_url(appid, fmt, assets.get("main_capsule")) or ""
    if not header and not main:
        raise ValueError(f"Steam did not return usable artwork for {appid}")
    description = {key: descriptions[key] for key in ("short_description", "short_description_en",
                                                       "short_description_language", "short_description_source")
                   if key in descriptions}
    return {
        "appid": appid,
        "name": name_en,
        "display_name": name_tw_traditional or name_cn_traditional or name_en,
        "display_name_source": ("tchinese" if name_tw_traditional else "schinese_converted"
                                if name_cn_traditional else "english"),
        "name_en": name_en,
        "name_zh_tw": name_tw,
        "name_zh_cn": name_cn,
        "name_zh_tw_traditional": name_tw_traditional,
        "name_zh_cn_traditional": name_cn_traditional,
        "steam_type": "game",
        "language_support": read_support(english),
        **category_fields(english, details, appid, checked_at),
        **reference_release(english, details, now),
        "release_date_timezone": "Asia/Taipei",
        "followers": None,
        "sexual_content_screened": True,
        "content_descriptorids": sorted({int(x) for x in english.get("content_descriptorids") or []
                                         if type(x) is int}),
        "header_image": header,
        "main_capsule_image": main,
        "header_image_2x": steam_asset_url(appid, fmt, assets.get("header_2x")) or "",
        "main_capsule_image_2x": steam_asset_url(appid, fmt, assets.get("main_capsule_2x")) or "",
        **description,
        **{key: taxonomy.get(key) or ([] if key in ("tags", "genres") else {}) for key in TAXONOMY_KEYS},
        "reference": {"method": METHOD, "igdb_ids": [row["igdb_id"] for row in links],
                      "console_titles": [row["name_en"] for row in links], "title_match": match},
        "checked_at": checked_at,
    }


def link_signature(links: list[dict]) -> list[list]:
    return [[row["igdb_id"], row["name_en"]] for row in links]


def due_at(checkpoint: dict, links: list[dict], has_record: bool) -> datetime:
    """When a reference is next checked: a new or changed console link at once."""
    if checkpoint.get("links") != link_signature(links):
        return MINIMUM_TIME
    checked = aware_time(checkpoint.get("checked_at"))
    attempted = aware_time(checkpoint.get("attempted_at"))
    if checkpoint.get("outcome") == "error" and attempted and (not checked or attempted >= checked):
        return aware_time(checkpoint.get("retry_at")) or attempted + RETRY_DELAY
    if checked is None or (checkpoint.get("outcome") == "accepted" and not has_record):
        return MINIMUM_TIME
    return checked + REFRESH_INTERVAL


def valid_record(record: object) -> bool:
    if not isinstance(record, dict) or type(record.get("appid")) is not int or record["appid"] <= 0:
        return False
    reference = record.get("reference")
    return (isinstance(reference, dict) and reference.get("method") == METHOD
            and isinstance(reference.get("igdb_ids"), list) and reference["igdb_ids"]
            and all(type(value) is int and value > 0 for value in reference["igdb_ids"])
            and record.get("sexual_content_screened") is True and record.get("followers") is None
            and isinstance(record.get("name"), str) and record["name"].strip() != ""
            and aware_time(record.get("checked_at")) is not None)


def valid_document(document: object) -> bool:
    return (isinstance(document, dict) and document.get("schema_version") == SCHEMA_VERSION
            and aware_time(document.get("generated_at")) is not None and isinstance(document.get("games"), list)
            and all(valid_record(row) for row in document["games"])
            and len({row["appid"] for row in document["games"]}) == len(document["games"]))


def references_document(records: list[dict], generated_at: str) -> dict:
    games = sorted(records, key=lambda row: row["appid"])
    return {"schema_version": SCHEMA_VERSION, "generated_at": generated_at, "method": METHOD,
            "count": len(games), "games": games}


def current_records(records: list[dict], links: dict[int, list[dict]], catalog: set[int], blocked: set[int]) -> list[dict]:
    """Records still justified by the latest console catalog and Steam catalog."""
    kept = []
    for record in records:
        appid = record["appid"]
        rows = links.get(appid)
        if (rows and appid not in catalog and appid not in blocked
                and record["reference"]["igdb_ids"] == [row["igdb_id"] for row in rows]
                and record["reference"].get("console_titles") == [row["name_en"] for row in rows]):
            kept.append(record)
    return kept
