"""Exercise canonical content sources, maintenance and accepted projections."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

NOW = datetime(2026, 10, 9, 15, 20, tzinfo=timezone.utc)
STAMP = "2026-10-09T15:20:00Z"


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz)


def source_item(language):
    return {
        "appid": 123,
        "success": 1,
        "name": "Example" if language == "english" else "範例遊戲",
        "release": {
            "coming_soon_display": "date_full",
            "steam_release_date": 1792790400,
        },
        "categories": {"supported_player_categoryids": [2, 9]},
        "supported_languages": [{"elanguage": 0, "supported": True}],
        "basic_info": {
            "short_description": (
                "An official story." if language == "english" else "这是一个游戏介绍"
            )
        },
        "assets": {
            "asset_url_format": "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/123/${FILENAME}",
            "header": "header.jpg",
        },
        "tagids": [19],
    }


class FakeResponse:
    status_code = 200

    def __init__(self, payload=None, text=""):
        self.payload, self.text = payload, text

    def raise_for_status(self):
        pass

    def json(self):
        return deepcopy(self.payload)


class FakeSession:
    def __init__(self):
        self.headers, self.calls, self.items = {}, [], []

    def get(self, url, *, params, timeout):
        self.calls.append((url, deepcopy(params), timeout))
        if "IStoreBrowseService" in url:
            language = json.loads(params["input_json"])["context"]["language"]
            item = source_item(language)
            self.items.append(deepcopy(item))
            return FakeResponse({"response": {"store_items": [item]}})
        if "appdetails" in url:
            return FakeResponse(
                {
                    "123": {
                        "success": True,
                        "data": {
                            "steam_appid": 123,
                            "type": "game",
                            "release_date": {"date": "23 Oct, 2026"},
                            "categories": [{"id": 2, "description": "Single-player"}],
                        },
                    }
                }
            )
        if "GetTagList" in url:
            return FakeResponse(
                {"response": {"tags": [{"tagid": 19, "name": "Adventure"}]}}
            )
        self.assert_store_url(url)
        return FakeResponse(
            text="""<html lang="zh-tw">InitAppTagModal(123, [{"tagid":19,"name":"冒險"}]);
<div id="genresAndManufacturer"><b>類型:</b><a href="https://store.steampowered.com/genre/Adventure/">冒險</a><br><div>"""
        )

    @staticmethod
    def assert_store_url(url):
        if url != "https://store.steampowered.com/app/123/":
            raise AssertionError("Unexpected source URL: " + url)


class ContentCompositionTests(unittest.TestCase):
    def test_canonical_graph_and_runtime_callbacks_need_no_legacy_owner(self):
        code = """
import importlib, importlib.abc, json, sys, tempfile
from pathlib import Path
owners = {'steam_player_modes', 'steam_taxonomy', 'localized_descriptions',
          'public_catalog', 'enrich_game', 'reconcile_catalog', 'twitch_steam_admission'}
blocked = owners | {'content_backend.' + name for name in owners}
class BlockLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise AssertionError('legacy dependency: ' + fullname)
sys.meta_path.insert(0, BlockLegacy())
for name in ('radar_backend.bootstrap', 'radar_backend.jobs.enrich_content',
             'radar_backend.jobs.reconcile_content', 'radar_backend.jobs.publish_content',
             'radar_backend.publication.catalog', 'radar_backend.publication.content_snapshot',
             'radar_backend.adapters.content_helpers', 'localize_taxonomy', 'localize_descriptions'):
    importlib.import_module(name)
from radar_backend.domain.player_categories import category_fields
from radar_backend.adapters import catalog_rules, localized_descriptions, steam_taxonomy, catalog_projection
assert category_fields({'appid':123,'success':1,'categories':{'supported_player_categoryids':[]}}, {},123,'2020-01-01T00:00:00Z')['categories'] == []
assert catalog_rules.keep_newer_release({}, {}) == {}
assert catalog_rules.metadata_gaps(None, {}) == ['missing_record']
assert steam_taxonomy.parse_store_taxonomy('<html lang="zh-tw">InitAppTagModal(123, []);',123,{})['tags'] == []
assert localized_descriptions.description_fields(123,'English source','這是官方中文介紹',None)['short_description_source'] == 'steam_tchinese'
assert isinstance(localized_descriptions.translations(),dict)
with tempfile.TemporaryDirectory() as directory:
    catalog_projection.write_catalog_projection(Path(directory),[],'2020-01-01T00:00:00Z')
    assert json.loads((Path(directory)/'catalog.json').read_text())['version'] == 3
assert not blocked.intersection(sys.modules)
"""
        root = Path(__file__).resolve().parents[1]
        env = dict(
            os.environ,
            PYTHONPATH=str(root / "content_backend"),
            PYTHONDONTWRITEBYTECODE="1",
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def build_and_publish(self, data):
        from radar_backend import bootstrap
        from radar_backend.publication import catalog
        from radar_backend.publication.content_snapshot import validate_snapshot

        data.mkdir(parents=True, exist_ok=True)
        (data / "excluded_appids.json").write_text('{"appids":[]}\n')
        session = FakeSession()
        with patch.object(bootstrap, "datetime", FrozenDateTime), patch.object(
            bootstrap.time, "sleep"
        ) as sleep:
            record = bootstrap.build_record(
                session,
                appid=123,
                followers=6000,
                event_release_date="2026-10-23",
                follower_checked_at=STAMP,
            )
        self.assertEqual([call.args for call in sleep.call_args_list], [(1.0,), (1.0,)])
        self.assertEqual(
            [
                json.loads(params["input_json"])["context"]["language"]
                for url, params, _ in session.calls
                if "IStoreBrowseService" in url
            ],
            ["english", "tchinese", "schinese"],
        )
        self.assertTrue(all(timeout == 35 for _, _, timeout in session.calls))
        self.assertEqual(
            session.items,
            [source_item(language) for language in ("english", "tchinese", "schinese")],
        )
        before = deepcopy(record)
        self.assertTrue(
            catalog.upsert_sharded(
                data, record, "2026-10-23", generated_at=STAMP, now=NOW
            )
        )
        self.assertEqual(record, before)
        validate_snapshot(data, now=NOW)
        return record

    def test_real_canonical_enrichment_flows_through_rules_and_coherent_projection(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            record = self.build_and_publish(data)
            self.assertEqual(record["short_description"], "這是一個遊戲介紹")
            self.assertEqual(record["short_description_source"], "steam_tchinese")
            self.assertEqual(record["tags"], ["Adventure"])
            self.assertEqual(record["tag_labels_zh_tw"], {"Adventure": "冒險"})
            self.assertEqual([row["id"] for row in record["categories"]], [2, 9])
            projection = json.loads((data / "catalog.json").read_text())
            shard = json.loads((data / "games/123.json").read_text())
            self.assertEqual(projection["version"], 3)
            self.assertEqual(projection["games"][0]["categories"], shard["categories"])
            self.assertNotIn("short_description", projection["games"][0])
            self.assertEqual(
                json.loads((data / "lists/upcoming.json").read_text())["appids"], [123]
            )

    def test_description_maintenance_preserves_eligibility_and_invalidates_full_record_revision(
        self,
    ):
        import localize_descriptions as maintenance
        from radar_backend.publication.content_snapshot import validate_snapshot

        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            self.build_and_publish(data)
            before = json.loads((data / "games/123.json").read_text())
            revision = json.loads((data / "catalog.json").read_text())["revision"]
            locales = {
                language: {
                    "123": {
                        "basic_info": {"short_description": "這是更新的官方中文介紹"}
                    }
                }
                for language in ("tchinese", "schinese")
            }
            with patch.object(
                maintenance, "fetch_locales", return_value=locales
            ), patch.object(maintenance, "utc_now", return_value=STAMP):
                result = maintenance.localize(data)
            after = json.loads((data / "games/123.json").read_text())
            description_fields = {
                "short_description",
                "short_description_en",
                "short_description_language",
                "short_description_source",
                "description_checked_at",
            }
            self.assertEqual(
                {k: v for k, v in after.items() if k not in description_fields},
                {k: v for k, v in before.items() if k not in description_fields},
            )
            self.assertEqual(after["short_description"], "這是更新的官方中文介紹")
            self.assertEqual(result, {"steam_tchinese": 1})
            self.assertNotEqual(
                json.loads((data / "catalog.json").read_text())["revision"], revision
            )
            validate_snapshot(data, now=NOW)

    def test_taxonomy_maintenance_uses_canonical_http_helpers_and_keeps_official_identity(
        self,
    ):
        import localize_taxonomy as maintenance
        from radar_backend.publication.content_snapshot import validate_snapshot

        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            self.build_and_publish(data)
            before = json.loads((data / "games/123.json").read_text())
            session = FakeSession()
            with patch.object(
                maintenance.requests, "Session", return_value=session
            ), patch.object(maintenance.time, "sleep"), patch.object(
                maintenance, "utc_now", return_value=STAMP
            ):
                result = maintenance.localize(data)
            after = json.loads((data / "games/123.json").read_text())
            for key in (
                "appid",
                "followers",
                "follower_checked_at",
                "release_start",
                "release_time_utc",
                "header_image",
                "language_support",
                "categories",
                "categories_source",
                "categories_checked_at",
                "short_description",
            ):
                self.assertEqual(after[key], before[key], key)
            self.assertEqual(result["completed"], 1)
            self.assertEqual(result["pending_count"], 0)
            self.assertEqual(
                [
                    params["language"]
                    for url, params, _ in session.calls
                    if "GetTagList" in url
                ],
                ["english"],
            )
            validate_snapshot(data, now=NOW)


if __name__ == "__main__":
    unittest.main()
