"""Published Chinese survives outages and upgrades independently of source changes."""

import unittest

from radar_backend.domain.localized_descriptions import merge_description_fields
from radar_backend.adapters.localized_descriptions import description_fields


def chinese(source="steam_machine_zh_tw", **extra):
    return {
        "short_description": "探索世界，展開冒險。",
        "short_description_en": "Explore the world.",
        "short_description_source": source,
        "short_description_language": "zh-TW",
        "description_source_url": "https://store.steampowered.com/app/123/",
        **extra,
    }


class DescriptionUpdateTests(unittest.TestCase):
    def test_official_chinese_replaces_machine_even_when_english_is_unchanged(self):
        old = chinese(description_translation_engine="argos", description_translation_version="old")
        official = chinese("steam_tchinese", short_description="官方新增繁體中文介紹。")
        merged = merge_description_fields(old, official)
        self.assertEqual(merged["short_description"], official["short_description"])
        self.assertEqual(merged["short_description_source"], "steam_tchinese")
        self.assertNotIn("description_translation_engine", merged)

    def test_lower_priority_snapshot_does_not_replace_official_chinese(self):
        old = chinese("steam_tchinese", description_checked_at="2026-10-15T01:00:00Z")
        incoming = chinese(short_description="較晚產生但較低優先的機器翻譯。", description_checked_at="2026-10-16T01:00:00Z")
        merged = merge_description_fields(old, incoming)
        self.assertEqual(merged["short_description"], old["short_description"])
        self.assertEqual(merged["short_description_source"], old["short_description_source"])

    def test_stale_queued_record_cannot_reset_description_retry_state(self):
        old = chinese(description_checked_at="2026-10-15T01:00:00Z", description_next_check_at="2026-10-16T01:00:00Z", description_retry_after="2026-10-15T07:00:00Z")
        incoming = chinese("steam_tchinese", short_description="過期的官方中文介紹。", description_checked_at="2026-10-14T01:00:00Z", description_next_check_at="2026-10-21T01:00:00Z", followers=9000)
        merged = merge_description_fields(old, incoming)
        for key in old:
            self.assertEqual(merged[key], old[key])
        self.assertEqual(merged["followers"], 9000)

    def test_failed_or_empty_translation_keeps_text_and_source_pair(self):
        old = chinese("steam_tchinese")
        unavailable = {"short_description_source": "unavailable", "short_description_en": "New English", "short_description": "", "short_description_language": ""}
        merged = merge_description_fields(old, unavailable)
        for key in old:
            self.assertEqual(merged[key], old[key])

    def test_english_is_valid_and_replaces_legacy_machine_translation(self):
        old = chinese(description_translation_engine="argos", description_translation_version="old", description_source_sha256="oldhash")
        english = description_fields(123, "Explore the new world.", None, None)
        merged = merge_description_fields(old, english)
        self.assertEqual(merged["short_description"], "Explore the new world.")
        self.assertEqual(merged["short_description_language"], "en")
        self.assertEqual(merged["short_description_source"], "steam_english")
        for key in ("description_translation_engine", "description_translation_version", "description_source_sha256"):
            self.assertNotIn(key, merged)

    def test_queued_legacy_machine_result_uses_english_instead(self):
        incoming = chinese(description_translation_engine="argos")
        merged = merge_description_fields({}, incoming)
        self.assertEqual(merged["short_description"], "Explore the world.")
        self.assertEqual(merged["short_description_language"], "en")
        self.assertEqual(merged["short_description_source"], "steam_english")
        self.assertNotIn("description_translation_engine", merged)

    def test_changed_english_invalidates_previous_editorial_chinese(self):
        old = chinese("editorial_zh_tw")
        english = description_fields(123, "Build a city.", None, None)
        merged = merge_description_fields(old, english)
        self.assertEqual(merged["short_description"], "Build a city.")
        self.assertEqual(merged["short_description_language"], "en")
        self.assertEqual(merged["short_description_source"], "steam_english")


if __name__ == "__main__":
    unittest.main()
