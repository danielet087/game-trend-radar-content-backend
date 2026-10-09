"""Pure content rules and transformation of an already collected Steam snapshot."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo
from radar_backend.domain.player_categories import category_fields
from radar_core.domain.twitch_admission import (
    TW_STORE_DATE_AUTHORITY, TW_STORE_DATE_PROVIDER, is_twitch_qualified,
    normalize_twitch_admission, resolve_store_release_day,
)

STORE_BROWSE = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
APPDETAILS = "https://store.steampowered.com/api/appdetails"
STORE_PAGE = "https://store.steampowered.com/app/{appid}/"
ASSET_BASE = "https://shared.akamai.steamstatic.com/store_item_assets/"
TAIPEI = ZoneInfo("Asia/Taipei")
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


def validate_enrichment_input(appid: int, followers: int, twitch_admission: dict | None) -> dict | None:
    admission = normalize_twitch_admission(twitch_admission, appid)
    if twitch_admission is not None and admission is None:
        raise RuntimeError('Invalid Twitch Steam admission proof')
    if type(followers) is not int or followers < 0 or (followers < 5000 and admission is None):
        raise RuntimeError('Steam Followers must be verified; low counts require Twitch admission')
    return admission


def validate_store_snapshot(appid: int, en: dict, details: dict, admission: dict | None) -> list:
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
    return descriptor_ids


@dataclass(frozen=True)
class ContentSnapshot:
    """Collected values only: this type has no HTTP client or filesystem path."""
    english: dict
    traditional: dict
    simplified: dict
    details: dict


def record_from_snapshot(
    snapshot: ContentSnapshot, *, appid: int, followers: int,
    event_release_date: str, follower_checked_at: str | None,
    allow_historical: bool, admission: dict | None, taxonomy: dict,
    descriptions: dict, checked_at: str, observed_now: datetime, convert,
) -> dict:
    en, tw, cn, details = snapshot.english, snapshot.traditional, snapshot.simplified, snapshot.details
    descriptor_ids = validate_store_snapshot(appid, en, details, admission)
    today = observed_now.astimezone(TAIPEI).date().isoformat()
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
        and event_release_date <= today
        and visible_release.get('coming_soon') is False
        and store_resolution is not None
        and store_resolution['release_start'] == event_release_date
    )
    historical_release = (
        (allow_historical or admission is not None)
        and valid_date(event_release_date)
        and (event_release_date < today
             or (admission is not None and event_release_date == today))
        and str(stamp).isdigit()
        and 0 < int(stamp) <= observed_now.timestamp()
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

    name_tw_traditional = convert(name_tw) if name_tw else None
    name_cn_traditional = convert(name_cn) if name_cn else None
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
        **category_fields(en, details, appid, checked_at),
        "release_raw": release_start,
        "release_start": release_start,
        "release_end": release_start,
        "release_precision": "day",
        "release_display_precision": "date_full",
        "release_date_timezone": "Asia/Taipei",
        "release_date_basis": "steam_store_browse_verified_full_date",
        "release_date_verified_at": None if release_date_conflict else checked_at,
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
        "artwork_checked_at": checked_at,
        "small_capsule_image": small_capsule,
        "store_url": STORE_PAGE.format(appid=appid),
        **descriptions,
        "description_checked_at": checked_at,
        **taxonomy,
        "content_enriched_at": checked_at,
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
            record['release_date_verified_at'] = checked_at
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


def taxonomy_fields(payload: dict | None, appid: int, checked_at: str) -> dict:
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
        "tags_checked_at": checked_at if tags_ok else None,
        "genres": payload["genres"] if genres_ok else [],
        "genre_labels_zh_tw": payload["genre_labels_zh_tw"] if genres_ok else {},
        "genre_labels_language": "zh-TW" if genres_ok else None,
        "genres_fetch_status": "ok" if genres_ok else "retry",
        "genres_source": payload.get("genres_source", source) if genres_ok else None,
        "genres_checked_at": checked_at if genres_ok else None,
    }
