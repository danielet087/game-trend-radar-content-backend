"""Store browser projections while preserving same-revision no-op writes."""

from pathlib import Path

from radar_backend.domain.catalog_projection import catalog_payload, catalog_revision


def write_catalog_projection(
    data_dir: Path,
    rows: list[dict],
    generated_at: str,
    *,
    fields,
    json_codec,
    hash_codec
) -> dict:
    revision = catalog_revision(rows, json_codec=json_codec, hash_codec=hash_codec)
    path = data_dir / "catalog.json"
    payload = catalog_payload(rows, generated_at, revision, fields=fields)
    if path.exists():
        try:
            if (
                json_codec.loads(path.read_text(encoding="utf-8")).get("revision")
                == revision
            ):
                return {"catalog_path": "catalog.json", "catalog_revision": revision}
        except (OSError, ValueError, TypeError):
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json_codec.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return {"catalog_path": "catalog.json", "catalog_revision": revision}
