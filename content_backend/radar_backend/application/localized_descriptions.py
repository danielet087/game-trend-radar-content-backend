"""Choose an official description before lazily loading editorial translations."""

from __future__ import annotations

from radar_backend.domain.localized_descriptions import localized_fields


def description_fields(
    appid: int,
    english: object,
    traditional: object,
    simplified: object,
    *,
    clean_description,
    is_chinese,
    convert,
    translations,
    fingerprint,
) -> dict:
    en, tw, cn = map(clean_description, (english, traditional, simplified))
    description, source = "", "unavailable"
    if is_chinese(tw):
        description, source = convert(tw), "steam_tchinese"
    elif is_chinese(cn):
        description, source = convert(cn), "steam_schinese_converted"
    else:
        translated = translations().get(str(appid), {})
        source_fingerprint = fingerprint(en)
        # Never carry a curated translation forward after its English source changes.
        if (
            en
            and translated.get("source_sha256") == source_fingerprint
            and is_chinese(translated.get("text", ""))
        ):
            description, source = convert(translated["text"]), "editorial_zh_tw"
    return localized_fields(en, description, source)
