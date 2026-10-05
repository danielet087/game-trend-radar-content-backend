import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from enrich_game import upsert_sharded
from reconcile_catalog import metadata_gaps, reconcile
from steam_player_modes import (
    APPDETAILS_SOURCE, BROWSE_SOURCE, category_fields, has_verified_categories,
    preserve_player_categories,
)

CHECKED = '2026-10-02T08:00:00Z'


def browse(ids):
    return {'appid': 123, 'success': 1, 'visible': True,
            'categories': {'supported_player_categoryids': ids,
                           'feature_categoryids': [22, 44, 62], 'controller_categoryids': [28]}}


def complete_record():
    return {'appid': 123, 'followers': 6000, 'follower_checked_at': CHECKED,
            'release_start': '2030-01-01', 'release_end': '2030-01-01',
            'release_display_precision': 'date_full', 'release_precision': 'day',
            'release_date_timezone': 'Asia/Taipei', 'release_time_utc': '2030-01-01T00:00:00Z',
            'release_timestamp_taipei_date': '2030-01-01', 'release_date_conflict': False,
            'header_image': 'known', 'artwork_checked_at': CHECKED,
            'description_checked_at': CHECKED, 'language_support': {'english': True},
            'tag_labels_language': 'zh-TW', 'genre_labels_language': 'zh-TW',
            'tags': ['Singleplayer'], 'tags_fetch_status': 'ok'}


class OfficialPlayerCategoryTests(unittest.TestCase):
    def test_browse_uses_matching_appid_player_ids_and_excludes_unrelated_features(self):
        fields = category_fields(browse([2, 9, 39, 9]), {}, 123, CHECKED)
        self.assertEqual([x['id'] for x in fields['categories']], [2, 9, 39])
        self.assertEqual(fields['categories_source'], BROWSE_SOURCE)
        self.assertTrue(has_verified_categories(fields))
        for item in ({**browse([1]), 'appid': 456}, {**browse([1]), 'appid': True},
                     {**browse([1]), 'success': 0}, {**browse([1]), 'visible': False},
                     browse([True]), browse(['1']), browse([-1])):
            self.assertEqual(category_fields(item, {}, 123, CHECKED), {})

    def test_appdetails_fallback_requires_exact_game_identity(self):
        details = {'steam_appid': 123, 'type': 'game',
                   'categories': [{'id': 1, 'description': 'Multi-player'}]}
        fields = category_fields({}, details, 123, CHECKED)
        self.assertEqual(fields['categories_source'], APPDETAILS_SOURCE)
        self.assertEqual(fields['categories'], details['categories'])
        for change in ({'steam_appid': 456}, {'steam_appid': True}, {'type': 'dlc'},
                       {'categories': [{'id': True, 'description': 'Multi-player'}]}):
            self.assertEqual(category_fields({}, {**details, **change}, 123, CHECKED), {})

    def test_checked_empty_list_differs_from_missing_unknown_data(self):
        empty = category_fields(browse([]), {}, 123, CHECKED)
        self.assertEqual(empty['categories'], [])
        self.assertTrue(has_verified_categories(empty))
        self.assertEqual(category_fields({'appid': 123, 'success': 1}, {}, 123, CHECKED), {})

    def test_untrusted_source_naive_invalid_and_future_times_are_not_verified(self):
        fields = category_fields(browse([1]), {}, 123, CHECKED)
        for change in ({'categories_source': 'community tags'}, {'categories_checked_at': 'checked'},
                       {'categories_checked_at': '2026-10-02T08:00:00'},
                       {'categories_checked_at': '2099-01-01T00:00:00Z'},
                       {'categories': [{'id': True, 'description': 'Multi-player'}]}):
            self.assertFalse(has_verified_categories({**fields, **change}))

    def test_partial_failure_preserves_verified_modes_and_new_negative_replaces_them(self):
        fields = category_fields(browse([1]), {}, 123, CHECKED)
        for incoming in ({}, {'categories': None}, {'categories': []}):
            self.assertEqual(preserve_player_categories(fields, incoming), fields)
        single = category_fields(browse([2]), {}, 123, CHECKED)
        self.assertEqual(preserve_player_categories(fields, single), single)

    def test_same_signature_backfill_updates_all_public_projections(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / 'excluded_appids.json').write_text('{"appids": []}')
            row = complete_record()
            upsert_sharded(data, row, row['release_start'])
            upgraded = {**row, **category_fields(browse([2, 1, 39]), {}, 123, CHECKED)}
            self.assertTrue(upsert_sharded(data, upgraded, row['release_start']))
            for path, collection in ((data / 'games/123.json', None),
                                     (data / 'calendar/2030-01.json', 'games'),
                                     (data / 'steam_upcoming.json', 'games'),
                                     (data / 'catalog.json', 'games')):
                document = json.loads(path.read_text())
                stored = document[collection][0] if collection else document
                self.assertEqual(stored['categories'], upgraded['categories'])
                self.assertEqual(stored['followers'], row['followers'])
                self.assertEqual(stored['release_start'], row['release_start'])
            self.assertFalse(upsert_sharded(data, upgraded, row['release_start']))

    def test_existing_content_queue_uses_category_only_fetch_and_preserves_date_followers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / 'data'
            data.mkdir()
            (data / 'excluded_appids.json').write_text('{"appids": []}')
            row = complete_record()
            upsert_sharded(data, row, row['release_start'])
            master = root / 'master.json'
            master.write_text(json.dumps({'games': [row]}))
            self.assertEqual(metadata_gaps(row, row), ['categories'])
            fields = category_fields(browse([2, 1]), {}, 123, CHECKED)
            with patch('reconcile_catalog.refresh_player_categories', return_value=fields) as fetch, \
                 patch('reconcile_catalog.build_record', side_effect=AssertionError('full refresh not needed')):
                status = reconcile(master, data, 60)
            fetch.assert_called_once()
            self.assertTrue(status['complete'])
            stored = json.loads((data / 'games/123.json').read_text())
            for key, value in row.items():
                self.assertEqual(stored[key], value)
            self.assertEqual(stored['categories'], fields['categories'])


if __name__ == '__main__':
    unittest.main()
