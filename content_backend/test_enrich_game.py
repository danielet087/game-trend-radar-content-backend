import json
import sys
import tempfile
import types
import unittest
from pathlib import Path


class _DummyOpenCC:
    def __init__(self, *_):
        pass

    def convert(self, value):
        return value


sys.modules.setdefault("opencc", types.SimpleNamespace(OpenCC=_DummyOpenCC))

from enrich_game import upsert_document, upsert_sharded, valid_date


class EnrichmentTests(unittest.TestCase):
    def test_dates(self):
        self.assertTrue(valid_date("2026-10-29"))
        self.assertFalse(valid_date("2026-Q4"))
        self.assertFalse(valid_date("2026-02-30"))

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
