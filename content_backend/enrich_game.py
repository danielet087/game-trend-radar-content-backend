from __future__ import annotations

import argparse
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests
from opencc import OpenCC
from public_catalog import keep_newer_release, write_catalog_projection
from localized_descriptions import description_fields, merge_description_fields
from steam_taxonomy import TAG_LIST, parse_store_taxonomy, preserve_taxonomy
from steam_player_modes import category_fields, has_verified_categories
from twitch_steam_admission import (
    TW_STORE_DATE_AUTHORITY, TW_STORE_DATE_PROVIDER, is_twitch_qualified,
    normalize_twitch_admission, resolve_store_release_day,
)

LOG = logging.getLogger(__name__)


class SteamRateLimit(RuntimeError):
    pass

STORE_BROWSE = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
APPDETAILS = "https://store.steampowered.com/api/appdetails"
STORE_PAGE = "https://store.steampowered.com/app/{appid}/"
ASSET_BASE = "https://shared.akamai.steamstatic.com/store_item_assets/"
TAIPEI = ZoneInfo("Asia/Taipei")
OPENCC = OpenCC("s2t")
HAN = re.compile(r"[\u3400-\u9fff]")
ALLOWED_ASSET_HOSTS = ("steamstatic.com", "steamcdn-a.akamaihd.net")
LANG_IDS = {"english": 0, "schinese": 6, "tchinese": 7}
OTHER_LANGUAGE_NAMES = {
    1: "德文", 2: "法文", 3: "義大利文", 4: "韓文", 5: "西班牙文",
    8: "俄文", 9: "泰文", 10: "日文", 11: "葡萄牙文", 12: "波蘭文",
    13: "丹麥文", 14: "荷蘭文", 15: "芬蘭文", 16: "挪威文", 17: "瑞典文",
    18: "匈牙利文", 19: "捷克文", 20: "羅馬尼亞文", 21: "土耳其文",
    22: "巴西葡萄牙文", 23: "保加利亞文", 24: "希臘文", 25: "阿拉伯文",
    26: "烏克蘭文", 27: "拉丁美洲西班牙文", 28: "越南文",
}
SEXUAL_CONTENT_IDS = frozenset({3, 4})
EXPLICIT_DESC = re.compile(
    r"\b(?:nsfw|hentai|pornograph(?:y|ic)|erotic(?:a)?|sex game|adult game|"
    r"sexually explicit|explicit sexual|uncensored sexual|sex scenes|"
    r"sexual acts|lots of sex)\b", re.I,
)


def is_explicit_sex_game(store_item: dict) -> bool:
    """Same formal rule as main backend screen_steam_candidates_before_followers."""
    ids = set(store_item.get('content_descriptorids') or [])
    if ids & SEXUAL_CONTENT_IDS:
        return True
    tags = [row.get('tagid') for row in (store_item.get('tags') or []) if isinstance(row, dict)]
    description = str((store_item.get('basic_info') or {}).get('short_description') or '')
    return bool(12095 in tags[:5] and (9130 in tags[:10] or 6650 in tags[:5]) and EXPLICIT_DESC.search(description))


def exact_store_display_date(value: object) -> str | None:
    """Accept only a full official day; never infer a quarter/month boundary."""
    if not isinstance(value, str):
        return None
    text = ' '.join(value.split()).strip()
    if valid_date(text):
        return text
    match = re.fullmatch(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', text)
    if match:
        candidate = f'{int(match[1]):04d}-{int(match[2]):02d}-{int(match[3]):02d}'
        return candidate if valid_date(candidate) else None
    for fmt in ('%d %b, %Y', '%d %B, %Y', '%b %d, %Y', '%B %d, %Y', '%d %b %Y', '%d %B %Y'):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def valid_date(value: object) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        return datetime.fromisoformat(value).date().isoformat() == value
    except ValueError:
        return False


def steam_asset_url(appid: int, fmt: object, filename: object) -> str:
    if not isinstance(fmt, str) or not isinstance(filename, str):
        return ""
    if "${FILENAME}" not in fmt or not filename or ".." in filename.split("/"):
        return ""
    url = urljoin(ASSET_BASE, fmt.replace("${FILENAME}", filename))
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not any(
        host == x or host.endswith("." + x) for x in ALLOWED_ASSET_HOSTS
    ):
        return ""
    if f"/steam/apps/{appid}/" not in parsed.path or ".." in parsed.path.split("/"):
        return ""
    return url if parsed.path.lower().endswith((".jpg", ".jpeg", ".png", ".webp")) else ""


def request_json(
    session: requests.Session, url: str, *, params: dict, attempts: int = 4
) -> dict:
    for attempt in range(attempts):
        try:
            response = session.get(url, params=params, timeout=35)
            if response.status_code == 429:
                raise SteamRateLimit("Steam rate limited this batch; defer remaining records")
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                return data
        except (requests.RequestException, ValueError, TypeError) as exc:
            LOG.warning(
                "Steam JSON request retry %d/%d: %s",
                attempt + 1, attempts, type(exc).__name__,
            )
            if attempt + 1 < attempts:
                time.sleep(4 * (attempt + 1))
    raise RuntimeError(f"Steam JSON request failed after {attempts} attempts: {url}")


def browse_one(session: requests.Session, appid: int, language: str) -> dict:
    payload = {
        "ids": [{"appid": appid}],
        "context": {"country_code": "TW", "language": language, "steam_realm": 1},
        "data_request": {
            "include_basic_info": True,
            "include_release": True,
            "include_assets": True,
            "include_supported_languages": True,
            "include_categories": True,
            "include_tag_count": 20,
        },
    }
    data = request_json(
        session,
        STORE_BROWSE,
        params={"input_json": json.dumps(payload, separators=(",", ":"))},
    )
    rows = (data.get("response") or {}).get("store_items") or []
    for row in rows:
        if isinstance(row, dict) and row.get("appid") == appid:
            return row
    raise RuntimeError(f"Steam Store Browse missing AppID {appid} in {language}")


def read_support(row: dict) -> dict:
    langs = row.get("supported_languages")
    if not isinstance(langs, list) or not langs:
        return {"tchinese": None, "schinese": None, "english": None}
    allowed = {
        x.get("elanguage")
        for x in langs
        if isinstance(x, dict)
        and type(x.get("elanguage")) is int
        and x.get("supported") is True
    }
    result = {
        "tchinese": LANG_IDS["tchinese"] in allowed,
        "schinese": LANG_IDS["schinese"] in allowed,
        "english": LANG_IDS["english"] in allowed,
    }
    if not any(result.values()):
        others = [
            OTHER_LANGUAGE_NAMES[x] for x in sorted(allowed)
            if x in OTHER_LANGUAGE_NAMES
        ]
        if others:
            result["other_languages"] = others
    return result


def official_chinese_title(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= 240 and HAN.search(value) else None


def refresh_player_categories(session: requests.Session, appid: int) -> dict:
    """One metadata request for an already qualified title; no date/Followers query."""
    item = browse_one(session, appid, "english")
    fields = category_fields(item, {}, appid, utc_now())
    if not fields:
        fields = category_fields({}, appdetails(session, appid), appid, utc_now())
    if not fields:
        raise RuntimeError(f"Steam player categories unavailable for AppID {appid}")
    return fields


def appdetails(session: requests.Session, appid: int, language: str = "tchinese") -> dict:
    """Best-effort appdetails enrichment.

    Store Browse is the required source for qualification, language and assets.
    appdetails is supplemental and can intermittently return success=false even
    for valid Store apps, so do not fail the whole content pipeline on that.
    """
    for attempt in range(3):
        try:
            data = request_json(
                session,
                APPDETAILS,
                params={"appids": appid, "cc": "TW", "l": language},
                attempts=2,
            )
        except SteamRateLimit:
            raise
        except RuntimeError as exc:
            LOG.warning("Steam appdetails transport failure for %s: %s", appid, exc)
            data = {}
        row = data.get(str(appid)) or {}
        if row.get("success") is True and isinstance(row.get("data"), dict):
            return row["data"]
        if attempt < 2:
            LOG.warning(
                "Steam appdetails unavailable for %s, retry %d/3",
                appid, attempt + 1,
            )
            time.sleep(5 * (attempt + 1))
    LOG.warning(
        "Steam appdetails unavailable for %s after retries; using Store Browse fallback",
        appid,
    )
    return {}


def fetch_taxonomy_from_api(session: requests.Session, appid: int, names: dict) -> dict | None:
    """Public Store metadata fallback, still Taiwan/Traditional Chinese."""
    localized = getattr(session, "_radar_tag_names_tw", None)
    if localized is None:
        data = request_json(session, TAG_LIST, params={"language": "tchinese"}, attempts=2)
        localized = {row["tagid"]: row["name"] for row in data.get("response", {}).get("tags", [])
                     if isinstance(row, dict) and type(row.get("tagid")) is int and isinstance(row.get("name"), str)}
        if not localized:
            return None
        session._radar_tag_names_tw = localized
    row = browse_one(session, appid, "tchinese")
    if row.get("success") != 1 or row.get("visible") is False or not isinstance(row.get("tagids"), list):
        return None
    ids = list(dict.fromkeys(x for x in row["tagids"] if type(x) is int and x > 0))[:20]
    if any(tag_id not in localized for tag_id in ids):
        return None
    tags = [names.get(tag_id) or f"steam-tag:{tag_id}" for tag_id in ids]
    result = {"tags": tags, "tag_ids": dict(zip(tags, ids)),
              "tag_labels_zh_tw": {name:localized[tag_id] for name,tag_id in zip(tags,ids)},
              "tags_source": STORE_BROWSE,
              "tag_labels_source": TAG_LIST + "?language=tchinese",
              "genres": None, "genre_labels_zh_tw": {}}
    details = appdetails(session, appid)
    genres = details.get("genres")
    if isinstance(genres, list):
        genre_names = getattr(session, "_radar_genre_names", {})
        if any(str(g.get("id")) not in genre_names for g in genres if isinstance(g, dict)):
            english = appdetails(session, appid, "english")
            genre_names.update({str(g["id"]):g["description"] for g in english.get("genres", [])
                                if isinstance(g, dict) and "id" in g and isinstance(g.get("description"), str)})
            session._radar_genre_names = genre_names
        result["genres"] = []
        for item in genres:
            if not isinstance(item, dict) or "id" not in item or not isinstance(item.get("description"), str):
                continue
            name = genre_names.get(str(item["id"])) or f"steam-genre:{item['id']}"
            result["genres"].append(name)
            result["genre_labels_zh_tw"][name] = item["description"]
        result["genres_source"] = APPDETAILS + f"?appids={appid}&cc=TW&l=tchinese"
    return result


def fetch_store_taxonomy(session: requests.Session, appid: int) -> dict | None:
    try:
        names = getattr(session, "_radar_tag_names", None)
        if names is None:
            data = request_json(session, TAG_LIST, params={"language": "english"}, attempts=2)
            names = {row["tagid"]: row["name"] for row in data.get("response", {}).get("tags", [])
                     if isinstance(row, dict) and type(row.get("tagid")) is int and isinstance(row.get("name"), str)}
            if not names:
                return None
            session._radar_tag_names = names
        response = session.get(
            STORE_PAGE.format(appid=appid),
            params={"cc": "TW", "l": "tchinese"},
            timeout=35,
        )
        if response.status_code == 429:
            raise SteamRateLimit("Steam tags rate limited this batch; defer remaining records")
        response.raise_for_status()
        payload = parse_store_taxonomy(response.text, appid, names)
        if payload is not None and isinstance(payload.get("genres"), list):
            return payload
        return fetch_taxonomy_from_api(session, appid, names) or payload
    except SteamRateLimit:
        raise
    except (requests.RequestException, RuntimeError):
        return None


def taxonomy_fields(payload: dict | None, appid: int) -> dict:
    source = STORE_PAGE.format(appid=appid) + "?cc=TW&l=tchinese"
    tags_ok = payload is not None
    genres_ok = tags_ok and isinstance(payload.get("genres"), list)
    return {
        "tags": payload["tags"] if tags_ok else [],
        "tag_ids": payload["tag_ids"] if tags_ok else {},
        "tag_labels_zh_tw": payload["tag_labels_zh_tw"] if tags_ok else {},
        "tag_labels_language": "zh-TW" if tags_ok else None,
        "tags_fetch_status": "ok" if tags_ok else "retry",
        "tags_source": payload.get("tags_source", source) if tags_ok else None,
        "tag_labels_source": payload.get("tag_labels_source", source) if tags_ok else None,
        "tags_checked_at": utc_now() if tags_ok else None,
        "genres": payload["genres"] if genres_ok else [],
        "genre_labels_zh_tw": payload["genre_labels_zh_tw"] if genres_ok else {},
        "genre_labels_language": "zh-TW" if genres_ok else None,
        "genres_fetch_status": "ok" if genres_ok else "retry",
        "genres_source": payload.get("genres_source", source) if genres_ok else None,
        "genres_checked_at": utc_now() if genres_ok else None,
    }


def build_record(
    session: requests.Session,
    *,
    appid: int,
    followers: int,
    event_release_date: str,
    follower_checked_at: str | None,
    allow_historical: bool = False,
    twitch_admission: dict | None = None,
) -> dict:
    admission = normalize_twitch_admission(twitch_admission, appid)
    if twitch_admission is not None and admission is None:
        raise RuntimeError('Invalid Twitch Steam admission proof')
    if type(followers) is not int or followers < 0 or (followers < 5000 and admission is None):
        raise RuntimeError('Steam Followers must be verified; low counts require Twitch admission')
    en = browse_one(session, appid, "english")
    time.sleep(1.0)
    tw = browse_one(session, appid, "tchinese")
    time.sleep(1.0)
    cn = browse_one(session, appid, "schinese")
    details = appdetails(session, appid)
    if admission is not None and details.get('type') != 'game':
        raise RuntimeError(f'Steam AppID {appid} is not a verified Steam game')
    if admission is not None and (type(details.get('steam_appid')) is not int or details['steam_appid'] != appid):
        raise RuntimeError(f'Steam appdetails identity does not match AppID {appid}')
    if admission is not None and (en.get('success') != 1 or en.get('visible') is False):
        raise RuntimeError(f'Steam AppID {appid} is unavailable in the Taiwan Store')
    descriptor_data = details.get("content_descriptors") or {}
    descriptor_ids = descriptor_data.get("ids", []) if isinstance(descriptor_data, dict) else []
    descriptor_ids = list(descriptor_ids) + list(en.get("content_descriptorids") or [])
    if {int(x) for x in descriptor_ids} & {3, 4}:
        raise RuntimeError(f"Steam AppID {appid} has adult-only sexual content descriptors")
    screened = dict(en)
    if not isinstance(screened.get('tags'), list):
        screened['tags'] = [{'tagid': tag_id} for tag_id in (en.get('tagids') or [])]
    if admission is not None and is_explicit_sex_game(screened):
        raise RuntimeError(f'Steam AppID {appid} failed the formal sexual-content screen')
    taxonomy = taxonomy_fields(fetch_store_taxonomy(session, appid), appid)
    if not taxonomy["tags"] and en.get("tagids"):
        taxonomy["tags_fetch_status"] = "retry"

    name_en = str(
        en.get("name") or details.get("name") or f"Steam App {appid}"
    ).strip()
    name_tw = official_chinese_title(tw.get("name"))
    name_cn = official_chinese_title(cn.get("name"))

    release = en.get("release") or {}
    stamp = release.get("steam_release_date")
    release_time_utc = None
    release_start = event_release_date
    release_timestamp_taipei_date = None
    if type(stamp) in (int, float) or (
        isinstance(stamp, str) and stamp.isdigit()
    ):
        instant = datetime.fromtimestamp(int(stamp), tz=timezone.utc)
        release_time_utc = instant.isoformat().replace("+00:00", "Z")
        release_timestamp_taipei_date = instant.astimezone(TAIPEI).date().isoformat()
    release_date_conflict = (
        release_timestamp_taipei_date is not None
        and release_timestamp_taipei_date != event_release_date
    )
    visible_release = details.get('release_date') or {}
    visible_release = visible_release if isinstance(visible_release, dict) else {}
    official_day = exact_store_display_date(visible_release.get('date'))
    store_resolution = (
        resolve_store_release_day(official_day, release_time_utc,
                                  allow_taiwan_store_authority=True)
        if admission is not None else None
    )
    # The TW Store's exact public announcement can disagree with Browse's
    # instant, including a released game whose Browse instant is still future.
    # Require the same AppID's visible Store date and released flag together.
    twitch_exact_released = (
        admission is not None
        and valid_date(event_release_date)
        and event_release_date <= datetime.now(TAIPEI).date().isoformat()
        and visible_release.get('coming_soon') is False
        and store_resolution is not None
        and store_resolution['release_start'] == event_release_date
    )
    historical_release = (
        (allow_historical or admission is not None)
        and valid_date(event_release_date)
        and (event_release_date < datetime.now(TAIPEI).date().isoformat()
             or (admission is not None and event_release_date == datetime.now(TAIPEI).date().isoformat()))
        and str(stamp).isdigit()
        and 0 < int(stamp) <= datetime.now(timezone.utc).timestamp()
        and not release.get("is_coming_soon", en.get("is_coming_soon", False))
        and not release.get("coming_soon_display")
    ) or twitch_exact_released
    if release.get("coming_soon_display") != "date_full" and not historical_release:
        raise RuntimeError(
            f"Steam AppID {appid} does not have a publicly announced exact Store date"
        )
    # An existing historical record can carry an old candidate date. Correct
    # it only when both fresh official sources agree on the same Taiwan day.
    # This is ordinary date verification, not the Twitch conflict exception.
    ordinary_exact_released = (
        admission is None and allow_historical and historical_release
        and details.get('type') == 'game'
        and type(details.get('steam_appid')) is int and details['steam_appid'] == appid
        and visible_release.get('coming_soon') is False
        and official_day is not None and official_day == release_timestamp_taipei_date
    )
    if admission is not None and release_time_utc is None:
        raise RuntimeError(f'Steam AppID {appid} release timestamp does not agree with the Taiwan date')
    release_date_resolution = None
    if ordinary_exact_released:
        release_start = official_day
        release_date_conflict = False
        release_date_resolution = resolve_store_release_day(official_day, release_time_utc)
    if admission is not None and (historical_release or release_date_conflict):
        release_date_resolution = store_resolution
        if (
            release_date_resolution is None
            or release_date_resolution['release_start'] != event_release_date
            or (historical_release and visible_release.get('coming_soon') is not False)
        ):
            if historical_release and not release_date_conflict:
                raise RuntimeError(f'Steam AppID {appid} does not have a matching exact released Store date')
            raise RuntimeError(f'Steam AppID {appid} release timestamp does not agree with the Taiwan date')

    assets = en.get("assets") or {}
    if not isinstance(assets, dict):
        assets = {}
    fmt = assets.get("asset_url_format")
    header = (
        steam_asset_url(appid, fmt, assets.get("header"))
        or str(details.get("header_image") or "")
    )
    main = steam_asset_url(appid, fmt, assets.get("main_capsule"))
    header_2x = steam_asset_url(appid, fmt, assets.get("header_2x"))
    main_2x = steam_asset_url(appid, fmt, assets.get("main_capsule_2x"))
    small_capsule = steam_asset_url(appid, fmt, assets.get("small_capsule"))
    # The 231x87 small capsule must never be the preferred public card image.
    capsule = main or header or small_capsule

    name_tw_traditional = OPENCC.convert(name_tw) if name_tw else None
    name_cn_traditional = OPENCC.convert(name_cn) if name_cn else None
    display_name = name_tw_traditional or name_cn_traditional or name_en
    display_name_source = (
        "tchinese" if name_tw_traditional
        else "schinese_converted" if name_cn_traditional
        else "english"
    )

    record = {
        "appid": appid,
        "name": name_en,
        "display_name": display_name,
        "display_name_source": display_name_source,
        "name_en": name_en,
        "name_zh_tw": name_tw,
        "name_zh_cn": name_cn,
        "name_zh_tw_traditional": name_tw_traditional,
        "name_zh_cn_traditional": name_cn_traditional,
        "name_en_traditional": name_en,
        "language_support": read_support(en),
        **category_fields(en, details, appid, utc_now()),
        "release_raw": release_start,
        "release_start": release_start,
        "release_end": release_start,
        "release_precision": "day",
        "release_display_precision": "date_full",
        "release_date_timezone": "Asia/Taipei",
        "release_date_basis": "steam_store_browse_verified_full_date",
        "release_date_verified_at": None if release_date_conflict else utc_now(),
        "release_time_utc": release_time_utc,
        "release_time_source": STORE_BROWSE,
        "release_timestamp_taipei_date": release_timestamp_taipei_date,
        "release_date_conflict": release_date_conflict,
        "followers": followers,
        "follower_checked_at": follower_checked_at,
        "follower_source": "Steam Community XML memberCount",
        "official_ge5000": followers >= 5000,
        "sexual_content_screened": True,
        "capsule_image": capsule,
        "header_image": header,
        "main_capsule_image": main,
        "header_image_2x": header_2x,
        "main_capsule_image_2x": main_2x,
        "artwork_checked_at": utc_now(),
        "small_capsule_image": small_capsule,
        "store_url": STORE_PAGE.format(appid=appid),
        **description_fields(
            appid,
            (en.get("basic_info") or {}).get("short_description"),
            (tw.get("basic_info") or {}).get("short_description") or details.get("short_description"),
            (cn.get("basic_info") or {}).get("short_description"),
        ),
        "description_checked_at": utc_now(),
        **taxonomy,
        "content_enriched_at": utc_now(),
        "content_enrichment_source": (
            "Steam Store Browse + "
            + ("appdetails + " if details else "appdetails fallback + ")
            + "Taiwan Traditional Chinese Store tags and genres"
        ),
        "content_enrichment_version": 5,
    }
    if details.get('type') == 'game':
        record['steam_type'] = 'game'
    if release_date_resolution is not None:
        record.update(release_date_resolution)
        if ordinary_exact_released or release_date_resolution['release_date_normalization'] == TW_STORE_DATE_AUTHORITY:
            record['release_display_provider'] = TW_STORE_DATE_PROVIDER
            record['release_date_verified_at'] = utc_now()
        if ordinary_exact_released:
            record['release_date_basis'] = 'steam_released_taiwan_store_date_verified'
    record['content_descriptorids'] = sorted({int(x) for x in descriptor_ids})
    if admission is not None:
        record['twitch_admission'] = admission
        if not is_twitch_qualified(record):
            raise RuntimeError(f'Steam AppID {appid} failed Twitch-source Steam qualification')
    if historical_release and admission is None and not ordinary_exact_released:
        # This permission is only for metadata on an already published title.
        # Keep its existing date evidence; do not pretend to re-verify a
        # coming-soon Store display that no longer exists after release.
        for key in ("release_display_precision", "release_date_basis", "release_date_verified_at"):
            record.pop(key, None)
    if not record["header_image"] and not record["main_capsule_image"]:
        raise RuntimeError(f"Steam did not return usable artwork for {appid}")
    if not isinstance(record["language_support"], dict):
        raise RuntimeError(f"Steam did not return language support for {appid}")
    return record


def upsert_document(path: Path, record: dict, event_release_date: str, *, force: bool = False) -> bool:
    doc = json.loads(path.read_text(encoding="utf-8"))
    games = doc.get("games")
    if not isinstance(games, list):
        raise RuntimeError(f"Invalid games array in {path}")
    appid = int(record["appid"])
    existing = next(
        (g for g in games if int(g.get("appid", -1)) == appid), None
    )
    record = keep_newer_release(existing or {}, record)
    if 'twitch_admission' in record and not is_twitch_qualified(record):
        raise RuntimeError(f'AppID {appid} has an invalid Twitch-source Steam qualification')
    signature = f"{appid}:{record['followers']}:{record['release_start']}"
    if existing:
        already = (
            existing.get("content_enrichment_signature") == signature
            and existing.get("header_image")
            and isinstance(existing.get("language_support"), dict)
            and isinstance(existing.get("tags"), list)
            and (int(existing.get("followers", 0)) >= 5000 or is_twitch_qualified(existing))
            and existing.get('twitch_admission') == record.get('twitch_admission')
            and (not has_verified_categories(record) or (
                has_verified_categories(existing) and existing['categories'] == record['categories']))
        )
        if already and not force:
            return False
        existing.update(preserve_taxonomy(existing, merge_description_fields(existing, record)))
        existing["content_enrichment_signature"] = signature
    else:
        row = dict(record)
        row["content_enrichment_signature"] = signature
        games.append(row)
    games.sort(key=lambda g: (
        str(g.get("release_start") or "9999-12-31"),
        int(g.get("appid", 0)),
    ))
    doc["games"] = games
    doc["count"] = len(games)
    doc["generated_at"] = utc_now()
    path.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return True



def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _update_month_file(data_dir: Path, month: str, appid: int, record: dict | None) -> None:
    path = data_dir / "calendar" / f"{month}.json"
    doc = {"version": 2, "month": month, "games": []}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                doc.update(loaded)
        except (OSError, ValueError, TypeError):
            pass
    games = [
        row for row in (doc.get("games") or [])
        if isinstance(row, dict) and int(row.get("appid", -1)) != appid
    ]
    if record is not None:
        games.append(record)
    games.sort(key=lambda g: (
        str(g.get("release_start") or "9999-12-31"),
        -int(g.get("followers") or 0),
        int(g.get("appid", 0)),
    ))
    if games:
        doc.update({
            "version": 2,
            "generated_at": utc_now(),
            "month": month,
            "count": len(games),
            "games": games,
        })
        _write_json(path, doc)
    elif path.exists():
        path.unlink()


def _rebuild_small_indexes(data_dir: Path) -> None:
    rows = []
    games_dir = data_dir / "games"
    for path in games_dir.glob("*.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(row, dict):
            continue
        try:
            appid = int(row.get("appid"))
            followers = int(row.get("followers"))
        except (TypeError, ValueError):
            continue
        release = row.get("release_start")
        if appid <= 0 or (followers < 3000 and not is_twitch_qualified(row)) or not valid_date(release):
            continue
        rows.append(row)

    rows.sort(key=lambda g: (
        str(g.get("release_start") or "9999-12-31"),
        -int(g.get("followers") or 0),
        int(g.get("appid", 0)),
    ))
    months = sorted({str(row["release_start"])[:7] for row in rows})
    now = utc_now()
    today = datetime.now(TAIPEI).date()
    today_s = today.isoformat()
    released_from = (today - timedelta(days=30)).isoformat()

    upcoming = [
        int(row["appid"]) for row in rows
        if row["release_start"] >= today_s and (int(row["followers"]) >= 5000 or is_twitch_qualified(row))
    ]
    released = [
        int(row["appid"]) for row in rows
        if released_from <= row["release_start"] < today_s
        and (
            int(row["followers"]) >= 5000
            or is_twitch_qualified(row)
            or (
                int(row["followers"]) > 3000
                and row.get("recent_source") in {"tracked_release", "direct_release"}
            )
        )
    ]

    prior_index = {}
    try:
        prior_index = json.loads((data_dir / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        prior_index = {}
    projection = write_catalog_projection(data_dir, rows, now)
    _write_json(data_dir / "index.json", {
        "version": 2,
        "generated_at": now,
        "source": "Steam AppID-sharded public catalog",
        "game_count": len(rows),
        "months": months,
        "calendar_path": "calendar/{YYYY-MM}.json",
        "game_path": "games/{appid}.json",
        "lists": {
            "upcoming": "lists/upcoming.json",
            "released": "lists/released.json",
        },
        "legacy_fallback": "steam_upcoming.json",
        "release_date_audited": prior_index.get("release_date_audited") is True,
        **projection,
    })
    _write_json(data_dir / "lists" / "upcoming.json", {
        "version": 2, "generated_at": now,
        "count": len(upcoming), "appids": upcoming,
    })
    _write_json(data_dir / "lists" / "released.json", {
        "version": 2, "generated_at": now,
        "count": len(released), "appids": released,
    })
    # Legacy readers must see exactly the same catalog as the sharded index.
    # Preserve released titles; do not derive this file from only upcoming AppIDs.
    _write_json(data_dir / "steam_upcoming.json", {
        "version": 2, "generated_at": now,
        "count": len(rows), "games": rows,
    })


def _shard_in_sync(data_dir: Path, record: dict) -> bool:
    """Existing AppID is not published until its month, indexes and fallback agree."""
    appid = int(record["appid"])
    release = str(record["release_start"])
    try:
        month = json.loads((data_dir / "calendar" / f"{release[:7]}.json").read_text(encoding="utf-8"))
        index = json.loads((data_dir / "index.json").read_text(encoding="utf-8"))
        legacy = json.loads((data_dir / "steam_upcoming.json").read_text(encoding="utf-8"))
        upcoming = json.loads((data_dir / "lists" / "upcoming.json").read_text(encoding="utf-8"))
        released = json.loads((data_dir / "lists" / "released.json").read_text(encoding="utf-8"))
        row = next(x for x in month["games"] if int(x["appid"]) == appid)
        fallback = next(x for x in legacy["games"] if int(x["appid"]) == appid)
        today = datetime.now(TAIPEI).date()
        day = datetime.fromisoformat(release).date()
        expected_upcoming = day >= today and (int(record["followers"]) >= 5000 or is_twitch_qualified(record))
        expected_released = (
            today - timedelta(days=30) <= day < today
            and (int(record["followers"]) >= 5000 or is_twitch_qualified(record) or (
                int(record["followers"]) > 3000
                and record.get("recent_source") in {"tracked_release", "direct_release"}
            ))
        )
        return (
            row == record
            and fallback == record
            and release[:7] in index["months"]
            and int(index["game_count"]) == int(legacy["count"]) == len(legacy["games"])
            and int(month["count"]) == len(month["games"])
            and (appid in upcoming["appids"]) == expected_upcoming
            and (appid in released["appids"]) == expected_released
        )
    except (OSError, ValueError, TypeError, KeyError, StopIteration):
        return False


def excluded_public_appids(data_dir: Path) -> set[int]:
    """Fail closed: never recreate games excluded by the official Steam adult audit."""
    path = data_dir / "excluded_appids.json"
    if not path.is_file():
        raise RuntimeError(f"Required adult exclusion list missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload.get("appids"), list):
        raise RuntimeError("Malformed adult exclusion list")
    return {int(appid) for appid in payload["appids"]}


def upsert_sharded(
    data_dir: Path,
    record: dict,
    event_release_date: str,
    *,
    force: bool = False,
) -> bool:
    """Update one AppID file and only the affected calendar shard."""
    appid = int(record["appid"])
    if appid in excluded_public_appids(data_dir):
        raise RuntimeError(f"AppID {appid} excluded by official Steam adult-content audit")
    path = data_dir / "games" / f"{appid}.json"
    existing = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, ValueError, TypeError):
            existing = {}

    record = keep_newer_release(existing, record)
    if 'twitch_admission' in record and not is_twitch_qualified(record):
        raise RuntimeError(f'AppID {appid} has an invalid Twitch-source Steam qualification')
    # An older queued event must not roll back a more recent official result.
    try:
        old_at = datetime.fromisoformat(str(existing.get("follower_checked_at")).replace("Z", "+00:00"))
        new_at = datetime.fromisoformat(str(record.get("follower_checked_at")).replace("Z", "+00:00"))
        if new_at < old_at and any(record.get(key) != existing.get(key) for key in ("followers", "release_start")):
            raise RuntimeError(f"Stale official event for AppID {appid}; preserving newer public data")
    except (ValueError, TypeError):
        pass

    signature = f"{appid}:{record['followers']}:{record['release_start']}"
    already = (
        existing.get("content_enrichment_signature") == signature
        and existing.get("header_image")
        and isinstance(existing.get("language_support"), dict)
        and isinstance(existing.get("tags"), list)
        and (int(existing.get("followers", 0)) >= 5000 or is_twitch_qualified(existing))
        and existing.get('twitch_admission') == record.get('twitch_admission')
        and (not has_verified_categories(record) or (
            has_verified_categories(existing) and existing['categories'] == record['categories']))
    )
    if already and not force:
        if _shard_in_sync(data_dir, existing):
            return False
        # A previous push may have published only the AppID file, or an
        # optimistic retry may have reset tracked indexes but kept an
        # untracked AppID. Repair indexes without another Steam request.
        _update_month_file(data_dir, str(existing["release_start"])[:7], appid, existing)
        _rebuild_small_indexes(data_dir)
        return True

    merged = preserve_taxonomy(existing, merge_description_fields(existing, record))
    merged["content_enrichment_signature"] = signature
    merged["storage_version"] = 2
    _write_json(path, merged)

    new_release = str(merged["release_start"])
    new_month = new_release[:7]
    # A prior partial publication can leave the AppID in an older month even
    # after its individual record moved. Remove every stale occurrence.
    for month_path in (data_dir / "calendar").glob("????-??.json"):
        if month_path.stem == new_month:
            continue
        month_doc = json.loads(month_path.read_text(encoding="utf-8"))
        if any(int(row.get("appid", -1)) == appid for row in month_doc.get("games", [])):
            _update_month_file(data_dir, month_path.stem, appid, None)
    _update_month_file(data_dir, new_month, appid, merged)
    _rebuild_small_indexes(data_dir)
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--appid", type=int, required=True)
    parser.add_argument("--followers", type=int, required=True)
    parser.add_argument("--release-date", required=True)
    parser.add_argument("--follower-checked-at")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument('--twitch-admission', type=Path, help='Verified Twitch discovery proof JSON')
    args = parser.parse_args()
    if args.appid <= 0:
        raise SystemExit("appid must be positive")
    admission = None
    if args.twitch_admission is not None:
        admission = normalize_twitch_admission(json.loads(args.twitch_admission.read_text(encoding='utf-8')), args.appid)
        if admission is None:
            raise SystemExit('Invalid Twitch Steam admission proof')
    if args.followers < 0 or (args.followers < 5000 and admission is None):
        raise SystemExit(
            "content backend only accepts official Followers >= 5000"
        )
    if not valid_date(args.release_date):
        raise SystemExit("release-date must be an exact YYYY-MM-DD")
    if not (args.data_dir / "index.json").exists():
        raise SystemExit(f"Missing sharded frontend index: {args.data_dir / 'index.json'}")

    if args.appid in excluded_public_appids(args.data_dir):
        raise SystemExit(f"AppID {args.appid} excluded by official Steam adult-content audit")

    session = requests.Session()
    session.headers["User-Agent"] = "GameTrendRadarContentBackend/1.0"
    record = build_record(
        session,
        appid=args.appid,
        followers=args.followers,
        event_release_date=args.release_date,
        follower_checked_at=args.follower_checked_at,
        twitch_admission=admission,
    )
    changed = upsert_sharded(
        args.data_dir, record, args.release_date, force=args.force
    )
    print(
        "CONTENT_ENRICHMENT_RESULT",
        json.dumps({
            "appid": args.appid,
            "followers": args.followers,
            "release_date": record["release_start"],
            "tags": len(record["tags"]),
            "genres": len(record["genres"]),
            "header": bool(record["header_image"]),
            "main_capsule": bool(record["main_capsule_image"]),
            "changed": changed,
            "force": args.force,
        }, ensure_ascii=False),
        flush=True,
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    main()
