"""Reviewed mature-game corrections reach content publication coherently."""
import json
from pathlib import Path
import tempfile
import unittest

from radar_core.domain.mature_content import REVIEWED_NON_EROTIC
from radar_backend.domain.content import is_explicit_sex_game, validate_store_snapshot
from radar_backend.publication.catalog import excluded_public_appids, upsert_sharded


class MatureContentReviewTests(unittest.TestCase):
    def test_reviewed_descriptor_four_survives_store_and_historical_public_exclusion(self):
        for appid in REVIEWED_NON_EROTIC:
            with self.subTest(appid=appid), tempfile.TemporaryDirectory() as tmp:
                data = Path(tmp)
                (data / "excluded_appids.json").write_text(json.dumps({"appids": [appid, 4005870]}))
                en = {"appid": appid, "content_descriptorids": [1, 4, 5]}
                descriptors = validate_store_snapshot(appid, en, {}, None)
                self.assertEqual(descriptors, [1, 4, 5])  # Keep the original evidence.
                self.assertFalse(is_explicit_sex_game(en))
                self.assertEqual(excluded_public_appids(data), {4005870})
                record = {"appid": appid, "followers": 6000, "release_start": "2099-01-01",
                          "content_descriptorids": descriptors}
                self.assertTrue(upsert_sharded(data, record, record["release_start"]))
                published = json.loads((data / "games" / f"{appid}.json").read_text())
                self.assertEqual(published["content_descriptorids"], [1, 4, 5])

    def test_new_adult_only_descriptor_blocks_reviewed_store_and_cached_record(self):
        for appid in REVIEWED_NON_EROTIC:
            with self.subTest(appid=appid), tempfile.TemporaryDirectory() as tmp:
                data = Path(tmp)
                (data / "excluded_appids.json").write_text(json.dumps({"appids": [appid]}))
                with self.assertRaisesRegex(RuntimeError, "adult-only"):
                    validate_store_snapshot(appid, {"appid": appid, "content_descriptorids": [3, 4]}, {}, None)
                with self.assertRaisesRegex(RuntimeError, "adult-content audit"):
                    upsert_sharded(data, {"appid": appid, "followers": 6000,
                        "release_start": "2099-01-01", "content_descriptorids": [3, 4]}, "2099-01-01")

    def test_new_explicit_primary_description_is_not_overridden_by_a_review(self):
        with self.assertRaisesRegex(RuntimeError, "formal sexual-content screen"):
            validate_store_snapshot(4310350, {"appid": 4310350, "content_descriptorids": [4],
                "tags": [{"tagid": 12095}, {"tagid": 9130}],
                "basic_info": {"short_description": "An explicit sex game"}}, {}, None)

    def test_unreviewed_explicit_descriptor_four_remains_excluded(self):
        with self.assertRaisesRegex(RuntimeError, "adult-only"):
            validate_store_snapshot(5189130, {"appid": 5189130, "content_descriptorids": [4]}, {}, None)


if __name__ == "__main__":
    unittest.main()
