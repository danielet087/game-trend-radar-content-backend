import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from enrich_game import build_record, upsert_sharded
from reconcile_catalog import metadata_gaps, reconcile


APPID = 4019220
CHECKED_AT = '2026-09-18T22:56:10Z'
RECHECKED_AT = '2026-10-03T14:31:41Z'


def existing_record():
    return {
        'appid': APPID, 'followers': 12629, 'follower_checked_at': CHECKED_AT,
        'release_start': '2026-09-22', 'release_end': '2026-09-22',
        'release_precision': 'day', 'release_display_precision': 'date_full',
        'release_time_utc': '2026-09-21T16:00:00Z',
        'release_timestamp_taipei_date': '2026-09-21', 'release_date_conflict': True,
        'header_image': 'known', 'artwork_checked_at': 'checked',
        'description_checked_at': 'checked', 'tag_labels_language': 'zh-TW',
        'genre_labels_language': 'zh-TW', 'language_support': {'english': True},
        'tags': ['Crafting'], 'tags_fetch_status': 'ok', 'content_enrichment_version': 4,
    }


def browse_record():
    return {
        'appid': APPID, 'success': 1, 'visible': True, 'name': 'Dressmaker',
        'release': {'steam_release_date': 1789990310},
        'supported_languages': [{'elanguage': 0, 'supported': True}],
        'assets': {'asset_url_format': f'https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{APPID}/${{FILENAME}}',
                   'header': 'header.jpg'},
    }


def details_record():
    return {'steam_appid': APPID, 'type': 'game',
            'release_date': {'coming_soon': False, 'date': '2026 年 9 月 21 日'}}


def make_fresh_record(details=None):
    with patch('enrich_game.browse_one', return_value=browse_record()), \
            patch('enrich_game.appdetails', return_value=details_record() if details is None else details), \
            patch('enrich_game.fetch_store_taxonomy', return_value={
                'tags': ['Crafting'], 'tag_ids': {'Crafting': 1702},
                'tag_labels_zh_tw': {'Crafting': '工藝'}, 'genres': [], 'genre_labels_zh_tw': {}}), \
            patch('enrich_game.time.sleep'), patch('enrich_game.utc_now', return_value=RECHECKED_AT):
        return build_record(object(), appid=APPID, followers=12629,
                            event_release_date='2026-09-22', follower_checked_at=CHECKED_AT,
                            allow_historical=True)


class ReleaseDateRepairTests(unittest.TestCase):
    def test_inconsistent_diagnostics_queue_fresh_recheck_without_relabeling_dates(self):
        row = existing_record()
        self.assertEqual(metadata_gaps(row, row), ['release_date_diagnostics'])
        self.assertEqual(row['release_start'], '2026-09-22')
        consistent = {**row, 'release_timestamp_taipei_date': '2026-09-22', 'release_date_conflict': False}
        self.assertEqual(metadata_gaps(consistent, consistent), [])
        genuine_conflict = {**consistent, 'release_start': '2026-09-23', 'release_date_conflict': True}
        self.assertEqual(metadata_gaps(genuine_conflict, genuine_conflict), [])
        naive = {**consistent, 'release_time_utc': '2026-09-21T16:00:00'}
        self.assertIn('release_date_diagnostics', metadata_gaps(naive, naive))

    def test_fresh_released_sources_correct_old_candidate_day_without_refreshing_followers(self):
        fresh = make_fresh_record()
        self.assertEqual(fresh['release_start'], '2026-09-21')
        self.assertEqual(fresh['release_end'], '2026-09-21')
        self.assertEqual(fresh['release_time_utc'], '2026-09-21T11:31:50Z')
        self.assertEqual(fresh['release_timestamp_taipei_date'], '2026-09-21')
        self.assertFalse(fresh['release_date_conflict'])
        self.assertEqual(fresh['release_store_date'], '2026-09-21')
        self.assertEqual(fresh['release_date_normalization'], 'steam_store_date_matches_taipei')
        self.assertEqual(fresh['release_display_provider'], 'Steam Store appdetails cc=TW l=tchinese')
        self.assertEqual(fresh['release_date_verified_at'], RECHECKED_AT)
        self.assertEqual(fresh['followers'], 12629)
        self.assertEqual(fresh['follower_checked_at'], CHECKED_AT)
        self.assertNotIn('twitch_admission', fresh)

    def test_ordinary_date_correction_requires_matching_id_type_released_flag_and_two_exact_days(self):
        details = details_record()
        invalid = [
            {**details, 'steam_appid': APPID + 1},
            {**details, 'steam_appid': True},
            {**details, 'type': 'dlc'},
            {**details, 'release_date': {'coming_soon': True, 'date': '2026 年 9 月 21 日'}},
            {**details, 'release_date': {'coming_soon': False, 'date': '2026 年 9 月'}},
            {**details, 'release_date': {'coming_soon': False, 'date': '2026 年 9 月 20 日'}},
        ]
        for value in invalid:
            with self.subTest(details=value):
                fresh = make_fresh_record(value)
                self.assertEqual(fresh['release_start'], '2026-09-22')
                self.assertTrue(fresh['release_date_conflict'])
                self.assertNotIn('release_date_verified_at', fresh)
                self.assertNotIn('release_date_normalization', fresh)

    def test_reconcile_bypasses_old_cache_repairs_all_projections_and_resists_old_master(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, cache = root / 'data', root / 'cache'
            data.mkdir(); cache.mkdir()
            (data / 'excluded_appids.json').write_text('{"appids": []}')
            old = existing_record()
            upsert_sharded(data, old, old['release_start'], force=True)
            master = root / 'master.json'
            master.write_text(json.dumps({'games': [old]}))
            signature = f"{APPID}:{old['followers']}:{old['release_start']}"
            (cache / f'{APPID}.json').write_text(json.dumps({'signature': signature, 'record': old}))
            fresh = make_fresh_record()
            with patch('reconcile_catalog.build_record', return_value=fresh) as build:
                status = reconcile(master, data, 1, cache_dir=cache)
            self.assertEqual(build.call_count, 1)
            self.assertTrue(build.call_args.kwargs['allow_historical'])
            self.assertTrue(status['complete'])
            for name in ['steam_upcoming.json', 'catalog.json', 'calendar/2026-09.json']:
                row = json.loads((data / name).read_text())['games'][0]
                self.assertEqual(row['release_start'], '2026-09-21', name)
                self.assertEqual(row['followers'], 12629, name)
                self.assertEqual(row['follower_checked_at'], CHECKED_AT, name)
            current = json.loads((data / 'games' / f'{APPID}.json').read_text())
            self.assertEqual(current['release_date_verified_at'], RECHECKED_AT)
            self.assertEqual(json.loads(master.read_text())['games'][0]['release_start'], '2026-09-22')
            with patch('reconcile_catalog.build_record', side_effect=AssertionError('already repaired')):
                self.assertTrue(reconcile(master, data, 1, cache_dir=cache)['complete'])
