"""Steam HTTP adapter; retry limits and source lookup stay here."""
from __future__ import annotations
import json
import logging
import time
import requests
from steam_taxonomy import TAG_LIST, parse_store_taxonomy
from radar_backend.domain.content import STORE_BROWSE, APPDETAILS, STORE_PAGE

LOG = logging.getLogger(__name__)

class SteamRateLimit(RuntimeError):
    pass

def request_json(
    session: requests.Session, url: str, *, params: dict, attempts: int = 4, sleep=time.sleep
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
                sleep(4 * (attempt + 1))
    raise RuntimeError(f"Steam JSON request failed after {attempts} attempts: {url}")


def browse_one(session: requests.Session, appid: int, language: str, *, request=request_json) -> dict:
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
    data = request(
        session,
        STORE_BROWSE,
        params={"input_json": json.dumps(payload, separators=(",", ":"))},
    )
    rows = (data.get("response") or {}).get("store_items") or []
    for row in rows:
        if isinstance(row, dict) and row.get("appid") == appid:
            return row
    raise RuntimeError(f"Steam Store Browse missing AppID {appid} in {language}")


def appdetails(session: requests.Session, appid: int, language: str = "tchinese", *, request=request_json, sleep=time.sleep) -> dict:
    """Best-effort appdetails enrichment.

    Store Browse is the required source for qualification, language and assets.
    appdetails is supplemental and can intermittently return success=false even
    for valid Store apps, so do not fail the whole content pipeline on that.
    """
    for attempt in range(3):
        try:
            data = request(
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
            sleep(5 * (attempt + 1))
    LOG.warning(
        "Steam appdetails unavailable for %s after retries; using Store Browse fallback",
        appid,
    )
    return {}


def fetch_taxonomy_from_api(session: requests.Session, appid: int, names: dict, *, request=request_json, browse=browse_one, details_lookup=appdetails) -> dict | None:
    """Public Store metadata fallback, still Taiwan/Traditional Chinese."""
    localized = getattr(session, "_radar_tag_names_tw", None)
    if localized is None:
        data = request(session, TAG_LIST, params={"language": "tchinese"}, attempts=2)
        localized = {row["tagid"]: row["name"] for row in data.get("response", {}).get("tags", [])
                     if isinstance(row, dict) and type(row.get("tagid")) is int and isinstance(row.get("name"), str)}
        if not localized:
            return None
        session._radar_tag_names_tw = localized
    row = browse(session, appid, "tchinese")
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
    details = details_lookup(session, appid)
    genres = details.get("genres")
    if isinstance(genres, list):
        genre_names = getattr(session, "_radar_genre_names", {})
        if any(str(g.get("id")) not in genre_names for g in genres if isinstance(g, dict)):
            english = details_lookup(session, appid, "english")
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


def fetch_store_taxonomy(session: requests.Session, appid: int, *, request=request_json, fallback=fetch_taxonomy_from_api) -> dict | None:
    try:
        names = getattr(session, "_radar_tag_names", None)
        if names is None:
            data = request(session, TAG_LIST, params={"language": "english"}, attempts=2)
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
        return fallback(session, appid, names) or payload
    except SteamRateLimit:
        raise
    except (requests.RequestException, RuntimeError):
        return None
