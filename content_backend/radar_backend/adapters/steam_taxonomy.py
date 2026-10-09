"""Public facade for pure Steam taxonomy rules."""

from __future__ import annotations

import html
import json
import re
from urllib.parse import unquote, urlsplit

from radar_backend.domain import steam_taxonomy as _taxonomy

TAG_LIST = _taxonomy.TAG_LIST
STORE_PAGE = _taxonomy.STORE_PAGE


def text(value: str) -> str:
    return _taxonomy.text(value, html_module=html, re_module=re)


def parse_store_taxonomy(
    body: str, appid: int, tag_names: dict[int, str]
) -> dict | None:
    return _taxonomy.parse_store_taxonomy(
        body,
        appid,
        tag_names,
        html_module=html,
        json_module=json,
        re_module=re,
        text_fn=text,
        unquote_fn=unquote,
        urlsplit_fn=urlsplit,
    )


def preserve_taxonomy(existing: dict, incoming: dict) -> dict:
    return _taxonomy.preserve_taxonomy(existing, incoming)
