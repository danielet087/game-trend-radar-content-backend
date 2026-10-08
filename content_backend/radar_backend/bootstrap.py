"""Production composition root; adapters depend on neither legacy CLI entry."""
from datetime import datetime, timezone
import time
import requests
from opencc import OpenCC
from localized_descriptions import description_fields
from radar_backend.adapters import steam_store
from radar_backend.application.enrichment import EnrichmentPorts, build_record as enrich, refresh_player_categories as refresh_categories
from radar_backend.application.reconciliation import ReconcilePorts, reconcile as reconcile_catalog
from radar_backend.publication import catalog
from radar_backend.state.json_documents import read_json, write_json

CONVERTER = OpenCC("s2t")


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_record(session, **kwargs):
    ports = EnrichmentPorts(steam_store.browse_one, steam_store.appdetails,
                            steam_store.fetch_store_taxonomy, description_fields,
                            CONVERTER.convert, time.sleep, utc_now,
                            lambda: datetime.now(timezone.utc))
    return enrich(session, ports=ports, **kwargs)


def refresh_player_categories(session, appid):
    return refresh_categories(session, appid, browse=steam_store.browse_one,
                              details=steam_store.appdetails, utc_now=utc_now)


def content_session():
    session = requests.Session()
    session.headers["User-Agent"] = "GameTrendRadarContentBackend/1.0"
    return session


def reconcile(master_path, data_dir, max_enrich, *, max_seconds=2400, cache_dir=None):
    ports = ReconcilePorts(read_json, write_json, catalog.excluded_public_appids,
                           build_record, refresh_player_categories, catalog.upsert_sharded,
                           catalog._shard_in_sync, catalog._rebuild_small_indexes,
                           requests.Session, time.monotonic, datetime.now, utc_now,
                           requests.RequestException, steam_store.SteamRateLimit)
    return reconcile_catalog(master_path, data_dir, max_enrich, ports=ports,
                             max_seconds=max_seconds, cache_dir=cache_dir)
