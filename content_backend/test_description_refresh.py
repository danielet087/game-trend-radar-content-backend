"""Refresh deadlines, independent retry state and frozen concurrent publication."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import localize_descriptions as cli
from radar_backend.domain.description_refresh import due_at, next_check, refresh_interval, select_due
from radar_backend.adapters.content_helpers import SteamRateLimit
from radar_backend.publication import catalog
from radar_backend.publication.description_snapshot import FrozenDescriptionPublication, OWNED_PATHS, freeze
from radar_backend.publication.content_snapshot import validate_snapshot
from radar_backend.state.json_documents import load_json, write_json
from radar_core.publication import SubprocessGitRepository, publish_with_retry
from test_content_publication import LocalGitFixture, record

NOW = datetime(2026, 10, 15, 1, 49, tzinfo=timezone.utc)


def locales(appids, english="Explore the world", traditional="", simplified=""):
    return {language: {str(appid): {"appid": appid, "basic_info": {"short_description": text}}
                       for appid in appids}
            for language, text in zip(cli.LANGUAGES, (english, traditional, simplified))}


class RefreshScheduleTests(unittest.TestCase):
    def test_release_window_and_official_sources(self):
        for day, source, interval in (("2026-12-15", "unavailable", 7),
                                      ("2026-12-14", "steam_english", 1),
                                      ("2026-09-15", "editorial_zh_tw", 1),
                                      ("2026-09-14", "steam_tchinese", 30),
                                      ("2026-11-01", "steam_schinese_converted", 7)):
            self.assertEqual(refresh_interval({"release_start": day, "short_description_source": source}, NOW), timedelta(days=interval))

    def test_weekly_deadline_does_not_skip_entry_to_daily_window(self):
        row = {"release_start": "2026-12-15", "short_description_source": "steam_english"}
        self.assertEqual(next_check(row, NOW), datetime(2026, 10, 15, 16, tzinfo=timezone.utc))

    def test_english_and_unchanged_hash_do_not_stop_official_checks(self):
        row = {"appid": 1, "release_start": "2026-11-01", "short_description_source": "steam_english",
               "description_checked_at": "2026-10-13T00:00:00Z", "short_description_source_sha256": "unchanged"}
        self.assertEqual(select_due([row], {}, NOW, 25), [row])
        row["description_checked_at"] = "2026-10-15T00:00:00Z"
        self.assertEqual(select_due([row], {}, NOW, 25), [])

    def test_failure_cooldown_and_newer_success_override(self):
        row = {"appid": 1, "release_start": "2026-11-01"}
        checkpoint = {"outcome": "error", "attempted_at": "2026-10-15T01:00:00Z", "retry_at": "2026-10-15T07:00:00Z"}
        self.assertEqual(due_at(row, checkpoint, NOW), datetime(2026, 10, 15, 7, tzinfo=timezone.utc))
        row["description_checked_at"] = "2026-10-15T01:30:00Z"
        self.assertEqual(due_at(row, checkpoint, NOW), datetime(2026, 10, 16, 1, 30, tzinfo=timezone.utc))


class DescriptionCollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        write_json(self.data / "excluded_appids.json", {"appids": []})

    def add(self, appid, **changes):
        row = record(appid, release_start="2026-11-01", short_description="舊的有效中文介紹",
                     short_description_source="steam_tchinese", short_description_en="Old English", **changes)
        catalog.upsert_sharded(self.data, row, row["release_start"], force=True, now=NOW)
        return load_json(self.data / "games" / f"{appid}.json")

    def test_429_keeps_existing_chinese_and_success_timestamp_and_retries_after_six_hours(self):
        before = self.add(123)
        with patch.object(cli, "fetch_locales", side_effect=SteamRateLimit("429")):
            result = cli.localize(self.data, now=NOW)
        self.assertEqual(load_json(self.data / "games/123.json"), before)
        self.assertEqual(result["stop_reason"], "steam_rate_limit")
        self.assertEqual(result["checkpoints"]["123"]["retry_at"], "2026-10-15T07:49:00Z")
        with patch.object(cli, "fetch_locales") as fetch:
            cli.localize(self.data, now=NOW + timedelta(hours=5))
        fetch.assert_not_called()

    def test_missing_one_language_preserves_only_that_game_and_keeps_others(self):
        before = self.add(123)
        self.add(456)
        response = locales([123, 456], traditional="新的官方繁中介紹")
        del response["schinese"]["123"]
        with patch.object(cli, "fetch_locales", return_value=response):
            result = cli.localize(self.data, now=NOW)
        self.assertEqual(result["failed_appids"], [123])
        self.assertEqual(result["updated_appids"], [456])
        self.assertEqual(load_json(self.data / "games/123.json"), before)
        self.assertEqual(load_json(self.data / "games/456.json")["short_description"], "新的官方繁中介紹")

    def test_fresh_english_and_new_official_chinese_replace_old_machine_without_other_changes(self):
        before = self.add(123)
        before.update(short_description_source="machine_translation", description_translation_engine="argos",
                      description_translation_version="old", description_source_sha256="oldhash")
        write_json(self.data / "games/123.json", before)
        with patch.object(cli, "fetch_locales", return_value=locales([123], english="New English", traditional="官方今天新增中文介紹")):
            cli.localize(self.data, now=NOW)
        after = load_json(self.data / "games/123.json")
        self.assertEqual(after["short_description_source"], "steam_tchinese")
        self.assertEqual(after["short_description_en"], "New English")
        for key in ("description_translation_engine", "description_translation_version", "description_source_sha256"):
            self.assertNotIn(key, after)
        for key in ("followers", "release_start", "header_image", "categories", "follower_checked_at", "content_enriched_at"):
            self.assertEqual(before[key], after[key])
        validate_snapshot(self.data, now=NOW)

    def test_no_due_games_still_migrate_derived_projection_from_valid_source_shards(self):
        self.add(123, description_checked_at="2026-10-15T00:00:00Z")
        old = load_json(self.data / "catalog.json")
        for row in old["games"]:
            for key in list(row):
                if key.startswith("short_description") or key.startswith("description_"):
                    del row[key]
        write_json(self.data / "catalog.json", old)
        with patch.object(cli, "fetch_locales") as fetch:
            cli.localize(self.data, now=NOW)
        fetch.assert_not_called()
        self.assertEqual(load_json(self.data / "catalog.json")["games"][0]["short_description"], "舊的有效中文介紹")
        validate_snapshot(self.data, now=NOW)

    def test_english_upgrades_when_official_chinese_arrives_without_english_change(self):
        before = self.add(123)
        before.update(short_description="Explore the world", short_description_en="Explore the world",
                      short_description_language="en", short_description_source="steam_english")
        write_json(self.data / "games/123.json", before)
        with patch.object(cli, "fetch_locales", return_value=locales([123], traditional="今天新增的官方中文介紹")):
            result = cli.localize(self.data, now=NOW)
        after = load_json(self.data / "games/123.json")
        self.assertEqual(after["short_description_source"], "steam_tchinese")
        self.assertEqual(after["short_description_language"], "zh-TW")
        self.assertEqual(after["short_description"], "今天新增的官方中文介紹")
        self.assertEqual(after["short_description_en"], before["short_description_en"])
        self.assertTrue(result["complete"])

    def test_bounded_batches_continue_remaining_without_refetching_success(self):
        for appid in range(1, 28):
            self.add(appid)
        batches = []
        def fetch(appids, cache=None):
            batches.append(appids)
            return locales(appids, traditional="新的官方繁中介紹")
        with patch.object(cli, "fetch_locales", side_effect=fetch):
            first = cli.localize(self.data, max_games=25, now=NOW)
            second = cli.localize(self.data, max_games=25, now=NOW)
        self.assertEqual([len(batch) for batch in batches], [25, 2])
        self.assertEqual(first["pending_count"], 2)
        self.assertEqual(second["pending_count"], 0)
        self.assertFalse(set(batches[0]) & set(batches[1]))

    def test_english_fallback_is_valid_and_does_not_use_old_translation_retry(self):
        before = self.add(123)
        before.update(short_description="舊的機器翻譯內容", short_description_source="steam_machine_zh_tw")
        write_json(self.data / "games/123.json", before)
        write_json(self.data / "description_refresh_status.json", {"checkpoints": {"123": {
            "outcome": "success", "attempted_at": "2020-01-01T00:00:00Z",
            "translation_pending": True, "translation_retry_at": "2020-01-01T06:00:00Z"}}})
        with patch.object(cli, "fetch_locales", return_value=locales([123])):
            result = cli.localize(self.data, now=NOW)
        row = load_json(self.data / "games/123.json")
        self.assertEqual(row["short_description"], "Explore the world")
        self.assertEqual(row["short_description_source"], "steam_english")
        self.assertEqual(row["short_description_language"], "en")
        self.assertTrue(result["complete"])
        self.assertEqual(result["pending_count"], 0)
        self.assertNotIn("translation_pending", result["checkpoints"]["123"])
        self.assertEqual(result["next_due_at"], "2026-10-16T01:49:00Z")
        with patch.object(cli, "fetch_locales") as fetch:
            cli.localize(self.data, now=NOW + timedelta(hours=7))
        fetch.assert_not_called()

    def test_time_budget_stops_before_the_next_game_not_only_the_next_batch(self):
        self.add(123)
        self.add(456)
        with patch.object(cli, "fetch_locales", return_value=locales([123, 456], traditional="新的官方繁中介紹")), \
             patch.object(cli.time, "monotonic", side_effect=[0, 0, 0, 600]):
            result = cli.localize(self.data, now=NOW)
        self.assertEqual(result["updated_appids"], [123])
        self.assertEqual(result["stop_reason"], "time_budget")

    def test_old_disk_locale_cache_cannot_suppress_a_fresh_request(self):
        write_json(self.data / "english.json", {"123": {"basic_info": {"short_description": "Old"}}})
        calls = []
        def request(session, url, *, params, attempts):
            payload = json.loads(params["input_json"])
            calls.append(payload)
            return {"response": {"store_items": [{"appid": 123, "basic_info": {"short_description": "New"}}]}}
        with patch.object(cli, "request_json", side_effect=request), patch.object(cli.time, "sleep"):
            result = cli.fetch_locales([123], self.data)
        self.assertEqual(len(calls), 3)
        self.assertEqual(result["english"]["123"]["basic_info"]["short_description"], "New")
        self.assertEqual([call["context"]["language"] for call in calls], list(cli.LANGUAGES))


class DescriptionPublicationTests(LocalGitFixture):
    def prepare(self):
        data = self.frontend / "data"
        with patch.object(cli, "fetch_locales", return_value=locales([101], traditional="新的官方繁中介紹")):
            cli.localize(data, force=True)
        return freeze(self.frontend, input_revision=self.git(self.frontend, "rev-parse", "HEAD"))

    def publish(self, frozen):
        replay = FrozenDescriptionPublication(frozen)
        receipt = publish_with_retry(SubprocessGitRepository(self.frontend, disposable_checkout=True), replay,
                                     paths=OWNED_PATHS, message="Descriptions", input_revision=frozen["input_revision"],
                                     payload_revision=replay.payload_revision, max_attempts=3)
        return receipt, replay

    def test_replay_preserves_concurrent_followers_dates_and_unrelated_games(self):
        frozen = self.prepare()
        newer = record(101, followers=9000, release_start="2099-02-01", follower_checked_at="2026-10-15T01:00:00Z",
                       content_enriched_at="2026-10-15T01:00:00Z", header_image="new.jpg")
        catalog.upsert_sharded(self.other / "data", newer, newer["release_start"], force=True)
        catalog.upsert_sharded(self.other / "data", record(456), "2099-01-01", force=True)
        self.commit(self.other, "Concurrent official evidence")
        self.git(self.other, "push", "origin", "HEAD:main")
        self.publish(frozen)
        row = self.remote_json("data/games/101.json")
        self.assertEqual(row["followers"], 9000)
        self.assertEqual(row["release_start"], "2099-02-01")
        self.assertEqual(row["header_image"], "new.jpg")
        self.assertEqual(row["short_description"], "新的官方繁中介紹")
        self.assertEqual(self.remote_json("data/games/456.json")["appid"], 456)

    def test_newer_description_check_supersedes_old_patch(self):
        frozen = self.prepare()
        newer = record(101, short_description="較新的官方介紹內容", short_description_source="steam_tchinese",
                       description_checked_at="2099-01-01T00:00:00Z")
        catalog.upsert_sharded(self.other / "data", newer, newer["release_start"], force=True)
        self.commit(self.other, "Newer description")
        self.git(self.other, "push", "origin", "HEAD:main")
        _, replay = self.publish(frozen)
        self.assertEqual(self.remote_json("data/games/101.json")["short_description"], "較新的官方介紹內容")
        self.assertEqual(replay.result["superseded_appids"], [101])


if __name__ == "__main__":
    unittest.main()
