import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class _DummyOpenCC:
    def __init__(self, *_):
        pass

    def convert(self, value):
        return value


sys.modules.setdefault("opencc", types.SimpleNamespace(OpenCC=_DummyOpenCC))

from enrich_game import build_record, upsert_document, upsert_sharded, valid_date


class EnrichmentTests(unittest.TestCase):
    def test_dates(self):
        self.assertTrue(valid_date("2026-10-29"))
        self.assertFalse(valid_date("2026-Q4"))
        self.assertFalse(valid_date("2026-02-30"))

    def test_build_record_keeps_verified_event_date_when_timestamp_rolls_over(self):
        release = {
            "coming_soon_display": "date_full",
            "steam_release_date": 1792790400,
        }
        english = {
            "appid": 123,
            "name": "Example",
            "release": release,
            "supported_languages": [{"elanguage": 0, "supported": True}],
            "assets": {
                "asset_url_format": "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/123/${FILENAME}",
                "header": "header.jpg",
                "main_capsule": "capsule_616x353.jpg",
            },
        }
        chinese = {
            **english,
            "name": "範例",
        }
        def fake_browse(_session, _appid, language):
            return english if language == "english" else chinese

        with patch("enrich_game.browse_one", side_effect=fake_browse), \
             patch("enrich_game.appdetails", return_value={}), \
             patch("enrich_game.fetch_tags", return_value=[]), \
             patch("enrich_game.time.sleep", return_value=None):
            row = build_record(
                object(),
                appid=123,
                followers=6000,
                event_release_date="2026-10-23",
                follower_checked_at=None,
            )
        self.assertEqual(row["release_start"], "2026-10-23")
        self.assertNotEqual(
            row["release_timestamp_taipei_date"], row["release_start"]
        )
        self.assertTrue(row["release_date_conflict"])

    def test_upsert_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "steam_upcoming.json"
            path.write_text(
                json.dumps({"count": 0, "games": []}),
                encoding="utf-8",
            )
            record = {
                "appid": 123,
                "followers": 6000,
                "release_start": "2026-10-29",
                "header_image": (
                    "https://shared.akamai.steamstatic.com/"
                    "store_item_assets/steam/apps/123/header.jpg"
                ),
                "language_support": {
                    "tchinese": True,
                    "schinese": True,
                    "english": True,
                },
                "tags": ["Action"],
            }
            self.assertTrue(
                upsert_document(path, record, "2026-10-29")
            )
            self.assertFalse(
                upsert_document(path, record, "2026-10-29")
            )
            self.assertTrue(
                upsert_document(path, record, "2026-10-29", force=True)
            )
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(doc["count"], 1)
            self.assertEqual(doc["games"][0]["appid"], 123)


    def test_sharded_upsert_updates_only_appid_and_month_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / "games").mkdir()
            (data / "calendar").mkdir()
            (data / "lists").mkdir()
            (data / "index.json").write_text(
                json.dumps({"version": 2, "months": []}),
                encoding="utf-8",
            )
            (data / "excluded_appids.json").write_text(
                json.dumps({"version": 1, "appids": [4005870]}),
                encoding="utf-8",
            )
            record = {
                "appid": 123,
                "followers": 6000,
                "release_start": "2026-10-29",
                "release_end": "2026-10-29",
                "release_precision": "day",
                "header_image": "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/123/header.jpg",
                "language_support": {"tchinese": True, "schinese": True, "english": True},
                "tags": ["Action"],
            }
            self.assertTrue(upsert_sharded(data, record, "2026-10-29"))
            self.assertFalse(upsert_sharded(data, record, "2026-10-29"))
            game = json.loads((data / "games" / "123.json").read_text(encoding="utf-8"))
            month = json.loads((data / "calendar" / "2026-10.json").read_text(encoding="utf-8"))
            index = json.loads((data / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(game["appid"], 123)
            self.assertEqual(month["games"][0]["appid"], 123)
            self.assertIn("2026-10", index["months"])


    def test_sharded_upsert_repairs_orphaned_game_and_legacy(self):
        # A concurrent publish retry can reset tracked indexes while leaving
        # a new, untracked games/<appid>.json in the checkout.
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / "games").mkdir()
            (data / "calendar").mkdir()
            (data / "lists").mkdir()
            (data / "index.json").write_text(
                json.dumps({"version": 2, "months": [], "game_count": 0}),
                encoding="utf-8",
            )
            (data / "steam_upcoming.json").write_text(
                json.dumps({"version": 2, "count": 0, "games": []}),
                encoding="utf-8",
            )
            (data / "excluded_appids.json").write_text(
                json.dumps({"version": 1, "appids": []}), encoding="utf-8",
            )
            record = {
                "appid": 2548880,
                "followers": 5463,
                "release_start": "2026-10-27",
                "release_end": "2026-10-27",
                "release_precision": "day",
                "release_display_precision": "date_full",
                "release_date_timezone": "Asia/Taipei",
                "header_image": "https://example.com/steam/header.jpg",
                "language_support": {"english": True},
                "tags": ["Horror"],
                "genres": ["Action"],
                "sexual_content_screened": True,
                "content_enrichment_signature": "2548880:5463:2026-10-27",
            }
            (data / "games" / "2548880.json").write_text(
                json.dumps(record), encoding="utf-8",
            )
            self.assertTrue(upsert_sharded(data, record, "2026-10-27"))
            month = json.loads((data / "calendar" / "2026-10.json").read_text(encoding="utf-8"))
            index = json.loads((data / "index.json").read_text(encoding="utf-8"))
            legacy = json.loads((data / "steam_upcoming.json").read_text(encoding="utf-8"))
            upcoming = json.loads((data / "lists" / "upcoming.json").read_text(encoding="utf-8"))
            self.assertEqual(month["count"], 1)
            self.assertEqual(index["game_count"], legacy["count"])
            self.assertEqual(index["game_count"], 1)
            self.assertIn(2548880, upcoming["appids"])
            self.assertFalse(upsert_sharded(data, record, "2026-10-27"))


    def test_sharded_upsert_never_readds_audited_adult_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / "excluded_appids.json").write_text(
                json.dumps({"version": 1, "appids": [4005870]}),
                encoding="utf-8",
            )
            adult = {
                "appid": 4005870,
                "followers": 7268,
                "release_start": "2026-10-16",
            }
            with self.assertRaisesRegex(RuntimeError, "excluded"):
                upsert_sharded(data, adult, "2026-10-16", force=True)
            self.assertFalse((data / "games" / "4005870.json").exists())


if __name__ == "__main__":
    unittest.main()
