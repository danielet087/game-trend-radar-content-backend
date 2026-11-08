"""Future games are classified consistently from intake to cached publication."""
import json
from pathlib import Path
import tempfile
import unittest

from radar_backend.domain.content import is_explicit_sex_game, validate_store_snapshot
from radar_backend.publication.catalog import excluded_public_appids, upsert_sharded


def ordinary(appid):
    return ({"appid": appid, "content_descriptorids": [1, 4, 5]},
        {"steam_appid": appid, "short_description": "A supernatural life simulation. Build a home and investigate ghosts.",
         "content_descriptors": {"ids": [1, 4, 5], "notes": "Optional shower nudity. No sexual acts."}})


class MatureContentReviewTests(unittest.TestCase):
    def test_new_and_legacy_mature_games_use_the_same_official_context(self):
        for appid in (4310350, 99000001, 99000002):
            with self.subTest(appid=appid), tempfile.TemporaryDirectory() as tmp:
                data = Path(tmp)
                legacy = [4310350, 4005870] if appid == 4310350 else [4005870]
                (data / "excluded_appids.json").write_text(json.dumps({"appids": legacy}))
                en, details = ordinary(appid)
                descriptors = validate_store_snapshot(appid, en, details, None)
                self.assertEqual(descriptors, [1, 4, 5, 1, 4, 5])
                self.assertFalse(is_explicit_sex_game({**en, "content_details": details}))
                self.assertEqual(excluded_public_appids(data), {4005870})
                record = {"appid": appid, "followers": 6000, "release_start": "2099-01-01",
                    "content_descriptorids": [1, 4, 5], "content_details": details}
                self.assertTrue(upsert_sharded(data, record, record["release_start"]))
                published = json.loads((data / "games" / f"{appid}.json").read_text())
                self.assertEqual(published["content_descriptorids"], [1, 4, 5])

    def test_adult_only_still_blocks_new_and_previously_cleared_games(self):
        for appid in (4310350, 99000001):
            with self.subTest(appid=appid), tempfile.TemporaryDirectory() as tmp:
                data = Path(tmp)
                (data / "excluded_appids.json").write_text('{"appids": []}')
                with self.assertRaisesRegex(RuntimeError, "adult-only"):
                    validate_store_snapshot(appid, {"appid": appid, "content_descriptorids": [3, 4]}, {}, None)
                with self.assertRaisesRegex(RuntimeError, "adult-content audit"):
                    upsert_sharded(data, {"appid": appid, "followers": 6000,
                        "release_start": "2099-01-01", "content_descriptorids": [3, 4]}, "2099-01-01")

    def test_primary_erotic_features_block_even_without_descriptor_three(self):
        en, details = ordinary(99000001)
        details["about_the_game"] = "Unlock 30 high-quality H-scenes through story progression."
        with self.assertRaisesRegex(RuntimeError, "formal sexual-content screen"):
            validate_store_snapshot(99000001, en, details, None)

    def test_another_games_adult_rating_is_pending_instead_of_a_porn_verdict(self):
        en, details = ordinary(99000001)
        details.update(steam_appid=99000002, content_descriptors={"ids": [3]})
        with self.assertRaisesRegex(RuntimeError, "content review pending"):
            validate_store_snapshot(99000001, en, details, None)

    def test_missing_context_stays_pending_then_recovers_on_a_later_lookup(self):
        en, details = ordinary(99000001)
        with self.assertRaisesRegex(RuntimeError, "content review pending"):
            validate_store_snapshot(99000001, en, {}, None)
        self.assertTrue(validate_store_snapshot(99000001, en, details, None))


if __name__ == "__main__":
    unittest.main()
