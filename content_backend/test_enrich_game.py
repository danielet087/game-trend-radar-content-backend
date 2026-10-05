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


try:
    import opencc
except ImportError:
    sys.modules.setdefault("opencc", types.SimpleNamespace(OpenCC=_DummyOpenCC))

from enrich_game import build_record, upsert_document, upsert_sharded, valid_date
from steam_player_modes import BROWSE_SOURCE

VERIFIED_CATEGORIES = {'categories': [{'id': 2, 'description': 'Single-player'}],
                       'categories_source': BROWSE_SOURCE, 'categories_checked_at': '2026-10-02T08:00:00Z'}


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
            "success": 1,
            "categories": {"supported_player_categoryids": [2, 9]},
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
             patch("enrich_game.fetch_store_taxonomy", return_value={"tags": [], "tag_ids": {}, "tag_labels_zh_tw": {}, "genres": [], "genre_labels_zh_tw": {}}), \
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
        self.assertIsNone(row["release_date_verified_at"])
        self.assertEqual([x['id'] for x in row['categories']], [2, 9])
        self.assertEqual(row['categories_source'], BROWSE_SOURCE)

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


class CompletenessTests(unittest.TestCase):
    def test_existing_header_does_not_hide_missing_tags_or_languages(self):
        from reconcile_catalog import metadata_gaps
        source = {'appid': 123, 'followers': 5000, 'release_start': '2026-10-20'}
        row = {**source, **VERIFIED_CATEGORIES, 'header_image': 'known', 'artwork_checked_at': 'checked', 'description_checked_at': 'checked', 'tag_labels_language':'zh-TW', 'genre_labels_language':'zh-TW'}
        self.assertEqual(metadata_gaps(row, source), ['languages', 'tags'])
        row.update(language_support={'english': True}, tags=[], tags_fetch_status='ok')
        self.assertEqual(metadata_gaps(row, source), [])
        row['tags_fetch_status'] = 'retry'
        self.assertEqual(metadata_gaps(row, source), ['tags'])

    def test_failed_tag_fetch_preserves_previous_tags_and_repairs_same_count_drift(self):
        from enrich_game import _shard_in_sync
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data/'excluded_appids.json').write_text('{"appids": []}')
            row = {'appid':123, 'followers':5000, 'release_start':'2026-10-20',
                   'header_image':'known', 'language_support':{'english':True},
                   'tags':['Action'], 'tags_fetch_status':'ok'}
            upsert_sharded(data, row, row['release_start'], force=True)
            upsert_sharded(data, {**row, 'tags':[], 'tags_fetch_status':'retry'}, row['release_start'], force=True)
            current = json.loads((data/'games/123.json').read_text())
            self.assertEqual(current['tags'], ['Action'])
            self.assertEqual(current['tags_fetch_status'], 'retry')
            legacy = json.loads((data/'steam_upcoming.json').read_text())
            legacy['games'][0]['followers'] = 9999
            (data/'steam_upcoming.json').write_text(json.dumps(legacy))
            self.assertFalse(_shard_in_sync(data, current))
            upsert_sharded(data, current, row['release_start'])
            self.assertTrue(_shard_in_sync(data, current))

    def test_projection_revision_changes_on_metadata_and_keeps_same_count(self):
        from public_catalog import write_catalog_projection
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            one = write_catalog_projection(data, [{'appid':123, 'tags':['Action']}], 'first')
            two = write_catalog_projection(data, [{'appid':123, 'tags':['Strategy']}], 'second')
            self.assertNotEqual(one['catalog_revision'], two['catalog_revision'])
            self.assertEqual(json.loads((data/'catalog.json').read_text())['count'], 1)

    def test_two_x_artwork_keeps_source_provided_hashes(self):
        from enrich_game import steam_asset_url
        fmt = 'steam/apps/123/${FILENAME}?t=7'
        self.assertEqual(steam_asset_url(123, fmt, 'hash/header_2x.jpg'),
          'https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/123/hash/header_2x.jpg?t=7')
        self.assertEqual(steam_asset_url(123, fmt, None), '')

    def test_released_metadata_requires_existing_public_record_permission(self):
        english = {'name': 'Released game', 'release': {'steam_release_date': 946684800},
                   'assets': {'asset_url_format': 'steam/apps/123/${FILENAME}', 'header': 'header.jpg'},
                   'supported_languages': [{'elanguage': 0, 'supported': True}]}
        with patch('enrich_game.browse_one', return_value=english), \
             patch('enrich_game.appdetails', return_value={}), \
             patch('enrich_game.fetch_store_taxonomy', return_value={'tags':['Action'], 'tag_ids':{'Action':19}, 'tag_labels_zh_tw':{'Action':'動作'}, 'genres':['Action'], 'genre_labels_zh_tw':{'Action':'動作'}}), \
             patch('enrich_game.time.sleep'):
            args = dict(appid=123, followers=6000, event_release_date='2000-01-01', follower_checked_at=None)
            with self.assertRaisesRegex(RuntimeError, 'exact Store date'):
                build_record(object(), **args)
            row = build_record(object(), **args, allow_historical=True)
            self.assertEqual(row['tags'], ['Action'])
            self.assertNotIn('release_date_verified_at', row)
            self.assertIsNone(row['follower_checked_at'])

    def test_reconcile_keeps_progress_and_reports_failed_record(self):
        from reconcile_catalog import reconcile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root/'data'
            data.mkdir()
            (data/'excluded_appids.json').write_text('{"appids": []}')
            rows = [{'appid': aid, 'followers': 6000, 'release_start': '2030-01-01',
                     'release_display_precision': 'date_full'} for aid in (123, 456)]
            master = root/'master.json'
            master.write_text(json.dumps({'games': rows}))
            enriched = {**rows[1], **VERIFIED_CATEGORIES, 'header_image': 'known', 'artwork_checked_at': 'checked', 'description_checked_at': 'checked', 'tag_labels_language':'zh-TW', 'genre_labels_language':'zh-TW',
                        'language_support': {'english': True}, 'tags': ['Action'], 'tags_fetch_status': 'ok'}
            with patch('reconcile_catalog.build_record', side_effect=[RuntimeError('temporarily unavailable'), enriched]):
                status = reconcile(master, data, 60)
            self.assertEqual(status['enriched'], 1)
            self.assertEqual(status['pending_count'], 1)
            self.assertIn('123', status['failures'])
            self.assertEqual(json.loads((data/'catalog.json').read_text())['count'], 1)

    def test_rate_limit_stops_fetches_but_still_repairs_local_indexes(self):
        from reconcile_catalog import reconcile
        from enrich_game import SteamRateLimit, _shard_in_sync
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root/'data'
            data.mkdir()
            (data/'excluded_appids.json').write_text('{"appids": []}')
            rows = [{'appid': aid, 'followers': 6000, 'release_start': '2030-01-01',
                     'release_display_precision': 'date_full'} for aid in (123, 456)]
            for row in rows:
                upsert_sharded(data, row, row['release_start'])
            (data/'calendar/2030-01.json').unlink()
            master = root/'master.json'
            master.write_text(json.dumps({'games': rows}))
            with patch('reconcile_catalog.build_record', side_effect=SteamRateLimit('429')) as fetch:
                status = reconcile(master, data, 60)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(status['pending_count'], 2)
            for row in rows:
                current = json.loads((data/'games'/f"{row['appid']}.json").read_text())
                self.assertTrue(_shard_in_sync(data, current))

    def test_stale_event_cannot_roll_back_newer_followers(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data/'excluded_appids.json').write_text('{"appids": []}')
            row = {'appid': 123, 'followers': 7000, 'release_start': '2030-01-01',
                   'follower_checked_at': '2026-09-28T00:00:00Z'}
            upsert_sharded(data, row, row['release_start'])
            with self.assertRaisesRegex(RuntimeError, 'Stale official event'):
                upsert_sharded(data, {**row, 'followers': 6000, 'follower_checked_at': '2026-09-27T00:00:00Z'}, row['release_start'])
            self.assertEqual(json.loads((data/'games/123.json').read_text())['followers'], 7000)

    def test_newer_date_audit_survives_old_master_and_cached_content(self):
        from public_catalog import keep_newer_release
        from reconcile_catalog import reconcile
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)/'data'
            data.mkdir()
            (data/'excluded_appids.json').write_text('{"appids": []}')
            old = {'appid': 123, 'followers': 7000, 'release_start': '2030-01-01',
                   'release_display_precision': 'date_full', 'release_date_verified_at': '2026-09-27T00:00:00Z'}
            current = {**old, **VERIFIED_CATEGORIES, 'release_start': '2030-01-02', 'release_date_verified_at': '2026-09-28T00:00:00Z',
                       'header_image': 'known', 'artwork_checked_at': 'checked', 'description_checked_at': 'checked', 'tag_labels_language':'zh-TW', 'genre_labels_language':'zh-TW',
                       'language_support': {'english': True}, 'tags': ['Action'], 'tags_fetch_status': 'ok'}
            upsert_sharded(data, current, current['release_start'])
            upsert_sharded(data, {**old, 'main_capsule_image_2x': 'new artwork'}, old['release_start'], force=True)
            result = json.loads((data/'games/123.json').read_text())
            self.assertEqual(result['release_start'], current['release_start'])
            self.assertEqual(result['main_capsule_image_2x'], 'new artwork')
            self.assertEqual(keep_newer_release(current, old)['release_start'], current['release_start'])
            master = Path(tmp)/'master.json'
            master.write_text(json.dumps({'games': [old]}))
            with patch('reconcile_catalog.build_record', side_effect=AssertionError('already complete')):
                self.assertTrue(reconcile(master, data, 60)['complete'])


if __name__ == "__main__":
    unittest.main()
