"""Replay frozen, validated content against the newest catalog without HTTP."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess

from radar_core.domain.twitch_admission import aware_time, is_twitch_qualified
from radar_core.publication import snapshot_revision
from radar_backend.adapters.catalog_rules import metadata_gaps, keep_newer_release
from radar_backend.domain.catalog import qualified_source
from radar_backend.domain.content import TAIPEI, valid_date
from radar_backend.publication import catalog
from radar_backend.state.json_documents import write_json
from radar_backend.domain.catalog_projection import FIELDS

OWNED_PATHS = (
    "data/games/", "data/calendar/", "data/lists/", "data/index.json",
    "data/steam_upcoming.json", "data/catalog.json", "data/content_refresh_status.json",
    "data/publication/steam_content.json",
    "data/publication/activity_origins.json",
)
MANIFEST_PATH = "publication/steam_content.json"
ACTIVITY_ORIGINS_PATH = "publication/activity_origins.json"
ACTIVITY_ORIGIN_LIMIT = 1000
EVENT_ACTIONS = frozenset({"", "workflow_dispatch", "steam_game_qualified",
                           "steam_game_twitch_discovered", "steam_game_refresh"})
UPDATE_REASONS = frozenset({"twitch_rankings", "steam_daily_search",
                            "steam_official_verification", "steam_metadata_refresh",
                            "steam_catalog_sync"})
PROVENANCE_FIELDS = ("source_repository", "source_workflow", "discovery_source", "source_run_id")


def _event_origin(event_action: str, source_provenance: dict | None = None) -> dict:
    """Freeze explicit trigger evidence; stored admission is not update evidence."""
    if not isinstance(event_action, str) or event_action not in EVENT_ACTIONS:
        raise RuntimeError("Unknown Steam content event action")
    if source_provenance is not None and not isinstance(source_provenance, dict):
        raise RuntimeError("Malformed content source provenance")
    provenance = {}
    for key, value in (source_provenance or {}).items():
        if key not in PROVENANCE_FIELDS or not isinstance(value, str) or len(value) > 500:
            raise RuntimeError("Malformed content source provenance field")
        if value:
            provenance[key] = value
    reason = {
        "steam_game_twitch_discovered": "twitch_rankings",
        "steam_game_qualified": "steam_official_verification",
        "steam_game_refresh": "steam_metadata_refresh",
        "workflow_dispatch": "steam_official_verification",
        "": "steam_metadata_refresh",  # Legacy freezes carry no precise trigger.
    }[event_action]
    # A missing admission or an official count never proves daily discovery.
    # Only a declared upstream discovery workflow can select this narrower label.
    if (event_action == "steam_game_qualified"
            and provenance.get("discovery_source") == "steam_daily_search"
            and provenance.get("source_repository") == "danielet087/game-trend-radar-backend"
            and provenance.get("source_workflow") == "steam-two-phase.yml"):
        reason = "steam_daily_search"
    return {"event_action": event_action, "source_provenance": provenance,
            "update_reason": reason}


def validate_activity_origins(value: dict) -> None:
    if (value.get("schema_version") != 1 or type(value.get("schema_version")) is not int
            or not isinstance(value.get("events"), list)):
        raise RuntimeError("Malformed activity origin history")
    for event in value["events"]:
        if (not isinstance(event, dict) or type(event.get("appid")) is not int or event["appid"] <= 0
                or event.get("type") not in {"added", "release_date"}
                or not valid_date(event.get("date")) or aware_time(event.get("at")) is None
                or event.get("update_reason") not in UPDATE_REASONS
                or (event["type"] == "release_date" and not valid_date(event.get("previous_date")))):
            raise RuntimeError("Malformed activity origin event")


def record_activity_origins(data_dir: Path, previous_catalog: dict, frozen: dict,
                            payload_revision: str) -> None:
    """Attribute only this attempt's actual accepted catalog additions/date changes."""
    previous = {row["appid"]: row for row in previous_catalog["games"]}
    current = load_json(data_dir / "catalog.json")["games"]
    path = data_dir / ACTIVITY_ORIGINS_PATH
    history = load_json(path) if path.is_file() else {"schema_version": 1, "events": []}
    validate_activity_origins(history)
    origin = frozen.get("activity_origin") or (
        {"update_reason": "steam_catalog_sync"} if frozen["kind"] == "reconciliation"
        else _event_origin(""))
    events = list(history["events"])
    ids = {event.get("id") for event in events if event.get("id")}
    for row in current:
        prior = previous.get(row["appid"])
        kind = "added" if prior is None else "release_date" if prior["release_start"] != row["release_start"] else None
        if kind is None:
            continue
        event = {"appid": row["appid"], "type": kind, "date": row["release_start"],
                 "update_reason": origin["update_reason"], "at": frozen["prepared_at"],
                 "payload_revision": payload_revision}
        if kind == "release_date":
            event["previous_date"] = prior["release_start"]
        event["id"] = snapshot_revision(event)[:24]
        if event["id"] not in ids:
            events.append(event)
            ids.add(event["id"])
    if events != history["events"]:
        write_json(path, {"schema_version": 1, "events": events[-ACTIVITY_ORIGIN_LIMIT:]})


def load_json(path: Path):
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError(f"Duplicate JSON key in {path}")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError(f"Non-finite JSON number in {path}")

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs,
                      parse_constant=invalid_constant)


def _owned(path: str) -> bool:
    return path.endswith(".json") and any(
        path.startswith(item) if item.endswith("/") else path == item
        for item in OWNED_PATHS
    )


def changed_paths(frontend: Path) -> list[str]:
    """Freeze may accept generated JSON, but never unrelated local modifications."""
    output = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=frontend, check=True, capture_output=True,
    ).stdout
    paths = []
    for entry in output.decode().split("\0"):
        if not entry:
            continue
        if len(entry) < 4 or any(flag in entry[:2] for flag in "RC"):
            raise RuntimeError("Content publication does not accept renamed files")
        path = entry[3:]
        if not _owned(path):
            raise RuntimeError(f"Unrelated dirty file cannot enter content publication: {path}")
        paths.append(path)
    return paths


def strict_catalog(data_dir: Path, *, require_index: bool = False) -> None:
    """Present but corrupt documents are fatal; absent derived files are repairable."""
    exclusion = load_json(data_dir / "excluded_appids.json")
    if not isinstance(exclusion, dict) or not isinstance(exclusion.get("appids"), list):
        raise RuntimeError("Malformed adult exclusion list")
    catalog.excluded_public_appids(data_dir)
    if require_index and not (data_dir / "index.json").is_file():
        raise RuntimeError("Missing sharded frontend index")
    paths = [data_dir / name for name in (
        "index.json", "catalog.json", "steam_upcoming.json", "content_refresh_status.json",
        "lists/upcoming.json", "lists/released.json", MANIFEST_PATH, ACTIVITY_ORIGINS_PATH,
    )]
    paths += list((data_dir / "games").glob("*.json"))
    paths += list((data_dir / "calendar").glob("*.json"))
    for path in paths:
        if not path.exists():
            continue
        value = load_json(path)
        if not isinstance(value, dict):
            raise RuntimeError(f"Malformed content document: {path}")
        if path.parent.name == "games":
            if not path.stem.isdigit() or type(value.get("appid")) is not int or value["appid"] <= 0 or str(value["appid"]) != path.stem:
                raise RuntimeError(f"Malformed AppID shard: {path}")
            if (not is_twitch_qualified(value) and (
                    type(value.get("followers")) is not int or value["followers"] < 0)
                    or not valid_date(value.get("release_start"))):
                raise RuntimeError(f"Malformed AppID source fields: {path}")
        if path.parent.name == "calendar" or path.name in {"catalog.json", "steam_upcoming.json"}:
            if not isinstance(value.get("games"), list) or any(not isinstance(row, dict) or type(row.get("appid")) is not int for row in value["games"]):
                raise RuntimeError(f"Malformed game collection: {path}")
        if path.parent.name == "calendar" and not re.fullmatch(r"\d{4}-\d{2}", path.stem):
            raise RuntimeError(f"Malformed calendar shard: {path}")
        if path.parent.name == "lists" and not isinstance(value.get("appids"), list):
            raise RuntimeError(f"Malformed AppID list: {path}")
        if path.name == "index.json" and (type(value.get("game_count")) is not int or not isinstance(value.get("months"), list)):
            raise RuntimeError(f"Malformed catalog index: {path}")
        if path.name == "content_refresh_status.json" and (type(value.get("complete")) is not bool or not isinstance(value.get("failures"), dict) or not isinstance(value.get("pending_enrichment"), dict)):
            raise RuntimeError(f"Malformed content status: {path}")
        if path.name == "activity_origins.json":
            validate_activity_origins(value)


def validate_record(record: dict) -> None:
    """The qualified-game event's existing hard contract, before freezing."""
    appid = record.get("appid")
    if type(appid) is not int or appid <= 0:
        raise RuntimeError("Invalid enriched AppID")
    if not is_twitch_qualified(record) and (
        type(record.get("followers")) is not int or record["followers"] < 5000
    ):
        raise RuntimeError("Unqualified enriched Followers")
    if (record.get("release_display_precision") != "date_full"
            or record.get("release_precision") != "day"
            or record.get("release_date_timezone") != "Asia/Taipei"
            or not valid_date(record.get("release_start"))
            or record.get("sexual_content_screened") is not True):
        raise RuntimeError("Invalid enriched date or adult-screening contract")
    if not (record.get("header_image") or record.get("main_capsule_image")):
        raise RuntimeError("Missing enriched artwork")
    if not isinstance(record.get("language_support"), dict) or not isinstance(record.get("tags"), list) or not isinstance(record.get("genres"), list):
        raise RuntimeError("Invalid enriched metadata")
    if record.get("content_enrichment_signature") != f"{appid}:{record['followers']}:{record['release_start']}":
        raise RuntimeError("Invalid enriched signature")


def _accepted_rows(data_dir: Path) -> list[dict]:
    rows = [load_json(path) for path in (data_dir / "games").glob("*.json")]
    rows = [row for row in rows if int(row["followers"] or 0) >= 3000 or is_twitch_qualified(row)]
    return sorted(rows, key=lambda row: (row["release_start"], -int(row["followers"] or 0), int(row["appid"])))


def validate_snapshot(data_dir: Path, *, now: datetime | None = None) -> None:
    """Projections must describe exactly the accepted shards, including v3 hash."""
    strict_catalog(data_dir, require_index=True)
    rows = _accepted_rows(data_dir)
    ids = {row["appid"] for row in rows}
    if ids & catalog.excluded_public_appids(data_dir):
        raise RuntimeError("Public catalog includes an adult-audit exclusion")
    index, legacy, projection = [load_json(data_dir / name) for name in ("index.json", "steam_upcoming.json", "catalog.json")]
    months = sorted({row["release_start"][:7] for row in rows})
    if legacy.get("games") != rows or legacy.get("count") != len(rows) or index.get("game_count") != len(rows) or index.get("months") != months:
        raise RuntimeError("Shards, index and legacy catalog disagree")
    for path in (data_dir / "calendar").glob("*.json"):
        month = load_json(path)
        expected = [row for row in rows if row["release_start"][:7] == path.stem]
        if month.get("games") != expected or month.get("count") != len(expected) or month.get("month") != path.stem:
            raise RuntimeError(f"Calendar shard disagrees with records: {path.name}")
    if any(not (data_dir / "calendar" / f"{month}.json").is_file() for month in months):
        raise RuntimeError("Missing calendar shard")
    today = (now or datetime.now(TAIPEI)).astimezone(TAIPEI).date()
    upcoming = [row["appid"] for row in rows if row["release_start"] >= today.isoformat() and ((row["followers"] or 0) >= 5000 or is_twitch_qualified(row))]
    released = [row["appid"] for row in rows if (today - timedelta(days=30)).isoformat() <= row["release_start"] < today.isoformat() and ((row["followers"] or 0) >= 5000 or is_twitch_qualified(row) or ((row["followers"] or 0) > 3000 and row.get("recent_source") in {"tracked_release", "direct_release"}))]
    for name, expected in (("upcoming", upcoming), ("released", released)):
        value = load_json(data_dir / "lists" / f"{name}.json")
        if value.get("appids") != expected or value.get("count") != len(expected):
            raise RuntimeError(f"{name} list disagrees with records")
    revision = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]
    projected_rows = [{key: row[key] for key in FIELDS if key in row} for row in rows]
    if (projection.get("version") != 3 or projection.get("games") != projected_rows
            or projection.get("revision") != revision or projection.get("count") != len(rows)
            or index.get("catalog_revision") != revision or index.get("catalog_path") != "catalog.json"):
        raise RuntimeError("Browser projection does not match accepted records")


def freeze_enrichment(frontend: Path, *, appid: int, followers: int | None,
                      event_release_date: str, force: bool, input_revision: str,
                      event_action: str = "", source_provenance: dict | None = None) -> dict:
    changed_paths(frontend)
    prepared_at = datetime.now(timezone.utc).replace(microsecond=0)
    data_dir = frontend / "data"
    strict_catalog(data_dir, require_index=True)
    record = load_json(data_dir / "games" / f"{appid}.json")
    validate_record(record)
    if record["appid"] != appid or record["followers"] != followers or not valid_date(event_release_date):
        raise RuntimeError("Enriched record does not match its event")
    validate_snapshot(data_dir, now=prepared_at)
    sources = {"frontend": input_revision}
    return {"schema_version": 1, "kind": "enrichment", "input_revision": snapshot_revision(sources), "source_revisions": sources,
            "prepared_at": prepared_at.isoformat(),
            "records": [record], "event_release_date": event_release_date, "force": force,
            "activity_origin": _event_origin(event_action, source_provenance)}


def freeze_reconciliation(frontend: Path, *, master_path: Path, input_revision: str,
                          source_revision: str) -> dict:
    paths = changed_paths(frontend)
    prepared_at = datetime.now(timezone.utc).replace(microsecond=0)
    data_dir = frontend / "data"
    strict_catalog(data_dir, require_index=True)
    master = load_json(master_path)
    if not isinstance(master, dict) or not isinstance(master.get("games"), list):
        raise RuntimeError("Malformed authoritative master")
    status = load_json(data_dir / "content_refresh_status.json")
    records = [load_json(frontend / path) for path in paths if path.startswith("data/games/") and (frontend / path).is_file()]
    validate_snapshot(data_dir, now=prepared_at)
    sources = {"frontend": input_revision, "master": source_revision}
    return {"schema_version": 1, "kind": "reconciliation", "input_revision": snapshot_revision(sources),
            "source_revisions": sources, "records": records, "master": master,
            "prepared_at": prepared_at.isoformat(),
            "collected_status": status,
            "activity_origin": {"update_reason": "steam_catalog_sync"}}


def _newer(existing: dict, incoming: dict) -> bool:
    """No queued record may roll back newer official or content evidence."""
    minimum = datetime.min.replace(tzinfo=timezone.utc)
    for field in ("follower_checked_at", "content_enriched_at"):
        old = aware_time(existing.get(field)) or minimum
        new = aware_time(incoming.get(field)) or minimum
        if old > new and (field != "follower_checked_at" or any(existing.get(key) != incoming.get(key) for key in ("followers", "release_start"))):
            return True
    merged = keep_newer_release(existing, incoming)
    return any(merged.get(key) != incoming.get(key) for key in ("release_start", "release_date_verified_at", "twitch_admission"))


def _documents(data_dir: Path) -> dict[str, dict]:
    return {str(path.relative_to(data_dir)): load_json(path)
            for path in data_dir.rglob("*.json")
            if _owned("data/" + str(path.relative_to(data_dir))) and str(path.relative_to(data_dir)) != MANIFEST_PATH}


def _keep_unchanged_times(data_dir: Path, before: dict[str, dict], original_text: dict[str, str]) -> None:
    for name, old in before.items():
        path = data_dir / name
        if not path.is_file() or "generated_at" not in old:
            continue
        current = load_json(path)
        if {key: value for key, value in current.items() if key != "generated_at"} == {key: value for key, value in old.items() if key != "generated_at"}:
            path.write_text(original_text[name], encoding="utf-8")


class FrozenContentPublication:
    """Per-attempt state is replaced only after successful local validation."""
    def __init__(self, frozen: dict):
        if frozen.get("schema_version") != 1 or frozen.get("kind") not in {"enrichment", "reconciliation"} or not isinstance(frozen.get("records"), list) or not isinstance(frozen.get("source_revisions"), dict):
            raise RuntimeError("Malformed frozen content batch")
        if frozen.get("input_revision") != snapshot_revision(frozen["source_revisions"]):
            raise RuntimeError("Frozen source revisions do not match input revision")
        if frozen["kind"] == "enrichment" and len(frozen["records"]) != 1:
            raise RuntimeError("Enrichment batch must contain exactly one record")
        if aware_time(frozen.get("prepared_at")) is None:
            raise RuntimeError("Frozen publication needs its preparation timestamp")
        if "activity_origin" in frozen:
            origin = frozen["activity_origin"]
            if not isinstance(origin, dict):
                raise RuntimeError("Malformed frozen activity origin")
            expected = ({"update_reason": "steam_catalog_sync"} if frozen["kind"] == "reconciliation"
                        else _event_origin(origin.get("event_action", ""), origin.get("source_provenance")))
            if origin != expected:
                raise RuntimeError("Frozen activity reason disagrees with its trigger evidence")
        self.frozen = copy.deepcopy(frozen)
        self.now = aware_time(frozen["prepared_at"])
        self.payload_revision = snapshot_revision(frozen)
        self.result = {}

    def __call__(self, frontend: Path) -> None:
        data_dir = frontend / "data"
        strict_catalog(data_dir, require_index=True)
        before = _documents(data_dir)
        previous_catalog = before.get("catalog.json") or {"games": _accepted_rows(data_dir)}
        original_text = {name: (data_dir / name).read_text(encoding="utf-8") for name in before}
        blocked = catalog.excluded_public_appids(data_dir)
        superseded = []
        for incoming in self.frozen["records"]:
            appid = int(incoming["appid"])
            if appid in blocked:
                raise RuntimeError(f"AppID {appid} excluded by latest adult audit")
            path = data_dir / "games" / f"{appid}.json"
            existing = load_json(path) if path.is_file() else {}
            if _newer(existing, incoming):
                if self.frozen["kind"] == "enrichment":
                    raise RuntimeError(f"Frozen content for AppID {appid} was superseded by newer public evidence")
                superseded.append(appid)
                continue
            if self.frozen["kind"] == "enrichment":
                validate_record(incoming)
            if incoming != existing or not catalog._shard_in_sync(data_dir, existing, now=self.now):
                catalog.upsert_sharded(data_dir, incoming, incoming["release_start"], force=True,
                                       generated_at=self.frozen["prepared_at"], now=self.now)
        if self.frozen["kind"] == "reconciliation":
            result = self._reconcile_local(data_dir, blocked, superseded)
        else:
            result = {"complete": True, "pending_count": 0, "superseded_appids": []}
        _keep_unchanged_times(data_dir, before, original_text)
        validate_snapshot(data_dir, now=self.now)
        record_activity_origins(data_dir, previous_catalog, self.frozen, self.payload_revision)
        target_snapshot = snapshot_revision(_documents(data_dir))
        manifest = {"schema_version": 1, "operation": self.frozen["kind"],
                    "input_revision": self.frozen["input_revision"],
                    "payload_revision": self.payload_revision,
                    "source_revisions": self.frozen["source_revisions"], "dataset_revision": target_snapshot,
                    "collection_complete": result["complete"], "superseded_appids": superseded}
        write_json(data_dir / MANIFEST_PATH, manifest)
        self.result = {**result, "dataset_revision": target_snapshot}

    def _reconcile_local(self, data_dir: Path, blocked: set[int], superseded: list[int]) -> dict:
        master = self.frozen.get("master")
        if not isinstance(master, dict) or not isinstance(master.get("games"), list):
            raise RuntimeError("Malformed frozen authoritative master")
        today = self.now.astimezone(TAIPEI).date().isoformat()
        qualified = {}
        for source in master["games"]:
            if not isinstance(source, dict):
                continue
            try:
                appid, followers = int(source["appid"]), (None if source["followers"] is None else int(source["followers"]))
            except (KeyError, ValueError, TypeError):
                continue
            day = source.get("release_start")
            path = data_dir / "games" / f"{appid}.json"
            historical = valid_date(day) and day < today and path.is_file()
            if qualified_source(source, appid, followers, day, historical=historical, blocked=blocked):
                record = load_json(path) if path.is_file() else {}
                latest_source = keep_newer_release(record, source)
                # The master's older official observation cannot manufacture gaps.
                if (aware_time(record.get("follower_checked_at")) or datetime.min.replace(tzinfo=timezone.utc)) > (aware_time(source.get("follower_checked_at")) or datetime.min.replace(tzinfo=timezone.utc)):
                    latest_source["followers"] = record["followers"]
                qualified[appid] = latest_source
        for appid in qualified:
            path = data_dir / "games" / f"{appid}.json"
            if path.is_file():
                record = load_json(path)
                if not catalog._shard_in_sync(data_dir, record, now=self.now):
                    catalog.upsert_sharded(data_dir, record, record["release_start"], force=True,
                                           generated_at=self.frozen["prepared_at"], now=self.now)
        catalog._rebuild_small_indexes(data_dir, generated_at=self.frozen["prepared_at"], now=self.now)
        pending = {}
        english_fallback = []
        for appid, source in qualified.items():
            path = data_dir / "games" / f"{appid}.json"
            record = load_json(path) if path.is_file() else None
            gaps = metadata_gaps(record, source)
            if gaps:
                pending[str(appid)] = gaps
            if record and record.get("short_description_language") == "en":
                english_fallback.append(appid)
        collected = self.frozen["collected_status"]
        status_path = data_dir / "content_refresh_status.json"
        latest = load_json(status_path) if status_path.is_file() else {}
        failures = dict(collected.get("failures") or {})
        minimum = datetime.min.replace(tzinfo=timezone.utc)
        for key, value in (latest.get("failures") or {}).items():
            if ((aware_time(value.get("attempted_at")) or minimum)
                    > (aware_time(failures.get(key, {}).get("attempted_at")) or minimum)):
                failures[key] = value
        metadata = latest if ((aware_time(latest.get("generated_at")) or minimum)
                              > (aware_time(collected.get("generated_at")) or minimum)) else collected
        metadata = {key: value for key, value in metadata.items() if key not in {"description_translation_pending", "description_translation_pending_count"}}
        result = {**metadata, "qualified_master": len(qualified), "pending_enrichment": pending,
                  "pending_count": len(pending), "public_count": load_json(data_dir / "index.json")["game_count"],
                  "complete": collected.get("complete") is True and not pending and not superseded,
                  "actual_catalog_complete": not pending, "superseded_appids": superseded,
                  "description_english_fallback": english_fallback,
                  "description_english_fallback_count": len(english_fallback),
                  "failures": {key: value for key, value in failures.items() if key in pending}}
        write_json(data_dir / "content_refresh_status.json", result)
        return result
