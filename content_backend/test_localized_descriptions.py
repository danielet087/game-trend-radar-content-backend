import json
import tempfile
import unittest
from pathlib import Path

from localized_descriptions import description_fields, merge_description_fields, translations
from enrich_game import upsert_sharded
from reconcile_catalog import metadata_gaps


class DescriptionTests(unittest.TestCase):
    def test_official_traditional_has_priority_and_html_is_plain_text(self):
        fields = description_fields(123, "English text", "<b>探索世界</b>，打造屬於你的冒險。", "探索世界，打造属于你的冒险。")
        self.assertEqual(fields["short_description"], "探索世界 ，打造屬於你的冒險。")
        self.assertEqual(fields["short_description_source"], "steam_tchinese")
        self.assertEqual(fields["short_description_language"], "zh-TW")

    def test_japanese_middle_dot_in_chinese_title_does_not_hide_chinese(self):
        fields = description_fields(123, "English", "《真・三國無雙》帶來一騎當千的極致體驗。", None)
        self.assertEqual(fields["short_description_source"], "steam_tchinese")

    def test_english_returned_for_tw_falls_back_to_converted_simplified(self):
        fields = description_fields(123, "Explore the world", "Explore the world", "探索广阔的世界，建设城镇，与动物成为朋友。")
        self.assertEqual(fields["short_description_source"], "steam_schinese_converted")
        self.assertIn("廣闊", fields["short_description"])
        self.assertIn("動物", fields["short_description"])
        self.assertNotIn("建设", fields["short_description"])

    def test_curated_translation_requires_exact_source_and_official_wins(self):
        source = translations()["4705510"]["source_text"]
        fields = description_fields(4705510, source, source, source)
        self.assertEqual(fields["short_description_source"], "editorial_zh_tw")
        self.assertIn("短暫的勝利", fields["short_description"])
        official = description_fields(4705510, source, "官方已經提供繁體中文的遊戲介紹。", source)
        self.assertEqual(official["short_description_source"], "steam_tchinese")
        changed = description_fields(4705510, source + " New feature.", source, source)
        self.assertEqual(changed["short_description_source"], "unavailable")
        self.assertEqual(changed["short_description"], "")
        self.assertEqual(changed["short_description_language"], "")

    def test_unsupported_language_is_not_mislabeled(self):
        for text in ("English description", "物語の世界を冒険するゲームです", ""):
            fields = description_fields(123, "English", text, text)
            self.assertEqual(fields["short_description_source"], "unavailable")
            self.assertEqual(fields["short_description"], "")

    def test_legacy_refresh_cannot_overwrite_chinese_and_explicit_unavailable_clears_it(self):
        chinese = description_fields(123, "English", "探索世界，享受冒險。", None)
        self.assertEqual(merge_description_fields(chinese, {"short_description": "English again"}), chinese)
        unavailable = description_fields(123, "Changed English", "Changed English", None)
        merged = merge_description_fields(chinese, unavailable)
        self.assertEqual(merged["short_description"], "")
        self.assertEqual(merged["short_description_language"], "")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "excluded_appids.json").write_text('{"appids": []}')
            base = {"appid": 123, "followers": 6000, "release_start": "2030-01-01"}
            upsert_sharded(root, {**base, **chinese}, base["release_start"])
            upsert_sharded(root, {**base, **unavailable}, base["release_start"], force=True)
            row = json.loads((root / "games/123.json").read_text())
            self.assertEqual(row["short_description"], "")
            self.assertEqual(row["short_description_source"], "unavailable")

    def test_collection_check_does_not_retry_forever_for_unavailable_chinese(self):
        base = {"appid": 123, "followers": 6000, "release_start": "2030-01-01", "header_image": "known",
                "artwork_checked_at": "checked", "tags": ["Action"], "language_support": {"english": True},
                "tag_labels_language": "zh-TW", "genre_labels_language": "zh-TW"}
        self.assertEqual(metadata_gaps(base, base), ["description_unchecked"])
        base.update(description_checked_at="checked", short_description_source="unavailable")
        self.assertEqual(metadata_gaps(base, base), [])


if __name__ == "__main__":
    unittest.main()
