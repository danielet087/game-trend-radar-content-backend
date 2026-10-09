"""Canonical runtime helpers shared by the content maintenance commands."""

from datetime import datetime, timezone
import time

from radar_backend.adapters import steam_store as _store
from radar_backend.domain.content import (
    STORE_BROWSE,
    taxonomy_fields as _taxonomy_fields,
)
from radar_backend.publication.catalog import upsert_sharded

SteamRateLimit = _store.SteamRateLimit


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def request_json(session, url, *, params: dict, attempts: int = 4) -> dict:
    return _store.request_json(
        session, url, params=params, attempts=attempts, sleep=time.sleep
    )


def browse_one(session, appid: int, language: str) -> dict:
    return _store.browse_one(session, appid, language, request=request_json)


def appdetails(session, appid: int, language: str = "tchinese") -> dict:
    return _store.appdetails(
        session, appid, language, request=request_json, sleep=time.sleep
    )


def fetch_taxonomy_from_api(session, appid: int, names: dict) -> dict | None:
    return _store.fetch_taxonomy_from_api(
        session,
        appid,
        names,
        request=request_json,
        browse=browse_one,
        details_lookup=appdetails,
    )


def fetch_store_taxonomy(session, appid: int) -> dict | None:
    return _store.fetch_store_taxonomy(
        session, appid, request=request_json, fallback=fetch_taxonomy_from_api
    )


def taxonomy_fields(payload: dict | None, appid: int) -> dict:
    return _taxonomy_fields(payload, appid, utc_now())
