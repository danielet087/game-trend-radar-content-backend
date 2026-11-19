"""Steam hero art, screenshots and trailers for one game's page.

These live in data/media/<appid>.json, apart from the game record: the record
is copied into every calendar shard and into the legacy catalog, while only
the game page needs these lists. Every URL points at Steam's own CDN; nothing
is re-hosted.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

from radar_backend.domain.content import ASSET_BASE, steam_asset_url

MEDIA_DIR = "media"
STATUS_FILE = "media_refresh_status.json"
MANIFEST = "publication/steam_media.json"
SCHEMA_VERSION = 1
REFRESH_INTERVAL = timedelta(days=7)
RETRY_DELAY = timedelta(hours=6)
MAX_SCREENSHOTS = 12
MAX_TRAILERS = 3
MAX_NAMES = 5
VIDEO_BASE = "https://video.akamai.steamstatic.com/store_trailers/"
MINIMUM_TIME = datetime.min.replace(tzinfo=timezone.utc)
CONTENT_KEYS = ("hero_image", "hero_image_2x", "screenshots", "trailers",
                "developers", "publishers", "franchises")

# Store paths: steam/apps/<numeric id>/<path>.<image>?t=<digits>
_IMAGE_PATH = re.compile(r"^steam/apps/\d+/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:jpg|jpeg|png)(?:\?t=\d+)?$")
_VIDEO_PATH = re.compile(r"^\d+/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+\.(?:mp4|webm|m3u8|mpd)$")


def aware_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _image(path: object) -> str:
    if not isinstance(path, str) or ".." in path.split("/") or not _IMAGE_PATH.match(path):
        return ""
    return ASSET_BASE + path


def _video(path: object) -> str:
    if not isinstance(path, str) or ".." in path.split("/") or not _VIDEO_PATH.match(path):
        return ""
    return VIDEO_BASE + path


def _screenshot(filename: object) -> dict | None:
    full = _image(filename)
    base, _, query = full.partition("?")
    if not full or not base.endswith(".jpg"):
        return None
    thumbnail = base[:-len(".jpg")] + ".600x338.jpg" + (f"?{query}" if query else "")
    return {"thumbnail": thumbnail, "full": full}


def _screenshots(item: dict) -> list[dict]:
    # Mature-content screenshots are never used, whatever the game's rating.
    rows = (item.get("screenshots") or {}).get("all_ages_screenshots")
    if not isinstance(rows, list):
        return []
    ordered = sorted((row for row in rows if isinstance(row, dict)),
                     key=lambda row: row.get("ordinal") if type(row.get("ordinal")) is int else 1 << 30)
    shots = []
    for row in ordered:
        shot = _screenshot(row.get("filename"))
        if shot and shot not in shots:
            shots.append(shot)
        if len(shots) == MAX_SCREENSHOTS:
            break
    return shots


def _trailer(row: dict) -> dict | None:
    if row.get("all_ages") is not True:
        return None
    fmt = row.get("trailer_url_format")
    if not isinstance(fmt, str) or "${FILENAME}" not in fmt:
        return None
    poster = _image(fmt.replace("${FILENAME}", str(row.get("screenshot_medium") or "")))
    poster_full = _image(fmt.replace("${FILENAME}", str(row.get("screenshot_full") or "")))
    sources = {}
    for clip in row.get("microtrailer") or []:
        if isinstance(clip, dict) and clip.get("type") in {"video/mp4", "video/webm"}:
            url = _video(clip.get("filename"))
            if url:
                sources["microtrailer_" + clip["type"].split("/")[1]] = url
    for stream in row.get("adaptive_trailers") or []:
        if isinstance(stream, dict) and stream.get("encoding") in {"hls_h264", "dash_h264"}:
            url = _video(stream.get("cdn_path"))
            if url:
                sources["hls" if stream["encoding"] == "hls_h264" else "dash"] = url
    if not poster or not sources:
        return None
    name = row.get("trailer_name")
    return {"name": name.strip()[:120] if isinstance(name, str) else "", "poster": poster,
            "poster_full": poster_full or poster, **dict(sorted(sources.items()))}


def _trailers(item: dict) -> list[dict]:
    groups = item.get("trailers") or {}
    rows = [row for key in ("highlights", "other_trailers") for row in (groups.get(key) or [])
            if isinstance(row, dict)]
    trailers = []
    for row in rows:
        trailer = _trailer(row)
        if trailer and trailer["poster"] not in {seen["poster"] for seen in trailers}:
            trailers.append(trailer)
        if len(trailers) == MAX_TRAILERS:
            break
    return trailers


def _names(rows: object) -> list[str]:
    names = []
    for row in rows if isinstance(rows, list) else []:
        name = row.get("name") if isinstance(row, dict) else None
        if isinstance(name, str) and name.strip() and name.strip() not in names:
            names.append(name.strip()[:120])
        if len(names) == MAX_NAMES:
            break
    return names


def media_content(appid: int, item: dict) -> dict:
    """The page-facing fields from one Store Browse item; no timestamps."""
    if not isinstance(item, dict) or item.get("appid") != appid:
        raise ValueError(f"Steam media item does not belong to AppID {appid}")
    assets = item.get("assets") if isinstance(item.get("assets"), dict) else {}
    fmt = assets.get("asset_url_format")
    info = item.get("basic_info") if isinstance(item.get("basic_info"), dict) else {}
    return {
        "hero_image": steam_asset_url(appid, fmt, assets.get("library_hero")) or None,
        "hero_image_2x": steam_asset_url(appid, fmt, assets.get("library_hero_2x")) or None,
        "screenshots": _screenshots(item),
        "trailers": _trailers(item),
        "developers": _names(info.get("developers")),
        "publishers": _names(info.get("publishers")),
        "franchises": _names(info.get("franchises")),
    }


def media_document(appid: int, content: dict, updated_at: str, source: str) -> dict:
    return {"schema_version": SCHEMA_VERSION, "appid": appid, "updated_at": updated_at,
            "source": source, **{key: content[key] for key in CONTENT_KEYS}}


def same_content(document: dict | None, content: dict) -> bool:
    return isinstance(document, dict) and all(document.get(key) == content[key] for key in CONTENT_KEYS)


def valid_document(document: object, appid: int) -> bool:
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        return False
    if document.get("appid") != appid or aware_time(document.get("updated_at")) is None:
        return False
    urls = [document.get("hero_image"), document.get("hero_image_2x")]
    urls += [shot.get(key) for shot in document.get("screenshots") or [] if isinstance(shot, dict)
             for key in ("thumbnail", "full")]
    urls += [value for trailer in document.get("trailers") or [] if isinstance(trailer, dict)
             for key, value in trailer.items() if key != "name"]
    allowed = (ASSET_BASE, VIDEO_BASE)
    return (isinstance(document.get("screenshots"), list) and isinstance(document.get("trailers"), list)
            and all(url is None or (isinstance(url, str) and url.startswith(allowed)) for url in urls)
            and all(isinstance(document.get(key), list) for key in ("developers", "publishers", "franchises")))


def retry_pending(appids, checkpoints: dict) -> list[int]:
    """Games whose latest attempt failed, even while their retry is not yet due."""
    return sorted(appid for appid in appids
                  if checkpoints.get(str(appid), {}).get("outcome") == "error")


def due_at(checkpoint: dict, has_document: bool) -> datetime:
    checked = aware_time(checkpoint.get("checked_at"))
    attempted = aware_time(checkpoint.get("attempted_at"))
    if checkpoint.get("outcome") == "error" and attempted and (not checked or attempted >= checked):
        return aware_time(checkpoint.get("retry_at")) or attempted + RETRY_DELAY
    if checked is None or not has_document:
        return MINIMUM_TIME
    return checked + REFRESH_INTERVAL
