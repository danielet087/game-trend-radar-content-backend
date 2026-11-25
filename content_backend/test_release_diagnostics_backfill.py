"""A record verified before the timestamp diagnostics existed must be able to complete.

Dragon Shelter (2712590) stayed in pending_enrichment from 11-15: its kept,
older date evidence won every merge and also dropped the freshly computed
diagnostics, so release_date_diagnostics could never clear.
"""
import json
import unittest
from copy import deepcopy

from public_catalog import keep_newer_release
from reconcile_catalog import metadata_gaps


def verified_before_diagnostics():
    return {
        'appid': 2712590, 'name': 'Dragon Shelter', 'followers': 8968,
        'follower_checked_at': '2026-09-19T00:50:10Z',
        'release_raw': '2026-09-25', 'release_start': '2026-09-25', 'release_end': '2026-09-25',
        'release_precision': 'day', 'release_display_precision': 'date_full',
        'release_date_timezone': 'Asia/Taipei', 'release_date_basis': 'steam_store_query_release_time',
        'release_date_verified_at': '2026-09-24T16:56:30Z', 'release_time_utc': '2026-09-24T17:00:00Z',
        'release_time_source': 'https://api.steampowered.com/IStoreQueryService/Query/v1/',
        'release_display_provider': 'Steam IStoreBrowseService/GetItems',
    }


def fresh_historical(existing, **diagnostics):
    """What enrichment builds for an already released title: date evidence left to the kept record."""
    row = deepcopy(existing)
    for key in ('release_display_precision', 'release_date_basis', 'release_date_verified_at'):
        row.pop(key, None)
    row.update({'release_timestamp_taipei_date': '2026-09-25', 'release_date_conflict': False, **diagnostics})
    return row


def source_of(record):
    return {'appid': record['appid'], 'release_start': record['release_start'], 'followers': record['followers']}


class ReleaseDiagnosticsBackfillTests(unittest.TestCase):
    def test_the_kept_record_gets_diagnostics_from_its_own_instant(self):
        existing = verified_before_diagnostics()
        merged = keep_newer_release(existing, fresh_historical(existing))
        self.assertEqual(merged['release_timestamp_taipei_date'], '2026-09-25')
        self.assertIs(merged['release_date_conflict'], False)
        for key in ('release_start', 'release_time_utc', 'release_date_verified_at', 'release_date_basis'):
            self.assertEqual(merged[key], existing[key])
        self.assertNotIn('release_date_diagnostics', metadata_gaps(merged, source_of(existing)))

    def test_a_kept_instant_on_another_taipei_day_is_a_conflict(self):
        existing = {**verified_before_diagnostics(), 'release_raw': '2026-09-24', 'release_start': '2026-09-24',
                    'release_end': '2026-09-24'}
        merged = keep_newer_release(existing, fresh_historical(existing))
        self.assertEqual(merged['release_timestamp_taipei_date'], '2026-09-25')
        self.assertIs(merged['release_date_conflict'], True)
        self.assertNotIn('release_date_diagnostics', metadata_gaps(merged, source_of(existing)))

    def test_records_with_diagnostics_or_without_an_instant_are_left_alone(self):
        recorded = {**verified_before_diagnostics(), 'release_timestamp_taipei_date': '2026-09-25',
                    'release_date_conflict': True}
        merged = keep_newer_release(recorded, fresh_historical(recorded))
        self.assertIs(merged['release_date_conflict'], True)  # an inconsistency stays visible as a gap
        self.assertIn('release_date_diagnostics', metadata_gaps(merged, source_of(recorded)))
        no_instant = verified_before_diagnostics()
        no_instant.pop('release_time_utc')
        merged = keep_newer_release(no_instant, fresh_historical(no_instant))
        self.assertNotIn('release_timestamp_taipei_date', merged)
        self.assertNotIn('release_date_conflict', merged)

    def test_a_newer_fresh_audit_still_wins_unchanged(self):
        existing = verified_before_diagnostics()
        incoming = {**fresh_historical(existing), 'release_display_precision': 'date_full',
                    'release_date_verified_at': '2026-11-18T05:30:00Z', 'release_date_conflict': False}
        merged = keep_newer_release(existing, incoming)
        self.assertEqual(merged['release_date_verified_at'], '2026-11-18T05:30:00Z')
        self.assertEqual(merged['release_timestamp_taipei_date'], '2026-09-25')


def store_day_admission():
    """Onimusha as published 11-22: the Store's 9/03 kept by an earlier audit;
    its release time is 9/04 12:02 in Taiwan."""
    return {
        'appid': 2638890, 'name': 'Onimusha: Way of the Sword', 'followers': 82455,
        'follower_checked_at': '2026-10-03T21:02:07.414834+08:00', 'sexual_content_screened': True,
        'steam_type': 'game', 'release_raw': '2026-09-03', 'release_start': '2026-09-03',
        'release_end': '2026-09-03', 'release_precision': 'day', 'release_display_precision': 'date_full',
        'release_date_timezone': 'Asia/Taipei', 'release_date_basis': 'steam_store_browse_verified_full_date',
        'release_date_verified_at': '2026-10-03T13:02:59Z', 'release_time_utc': '2026-09-04T04:02:14Z',
        'release_time_source': 'https://api.steampowered.com/IStoreBrowseService/GetItems/v1/',
        'release_timestamp_taipei_date': '2026-09-04', 'release_date_conflict': True,
        'release_store_date': '2026-09-03', 'release_date_normalization': 'steam_taiwan_store_date_authoritative',
        'release_display_provider': 'Steam Store appdetails cc=TW l=tchinese',
        'twitch_admission': {
            'appid': 2638890, 'checked_at': '2026-10-03T10:10:30Z', 'igdb_id': '325602',
            'method': 'twitch_igdb_external_steam_v1', 'schema_version': 1,
            'source_enrollment': {'kind': 'history_recovery', 'min_viewers': 7000,
                                  'observed_at': '2026-09-30T00:56:47Z', 'source': 'igdb_first_release_date',
                                  'viewer_count': 11331},
            'source_frontend_commit': '38239a15050deb2331ede8ef2ea2e23e9beb2d64', 'twitch_game_id': '327598602'},
    }


class KeptStoreDayTests(unittest.TestCase):
    def test_a_kept_store_day_one_day_off_takes_taiwans_day(self):
        existing = store_day_admission()
        # The master moved to Taiwan's day but its confirmation is a few seconds older.
        master = {**deepcopy(existing), 'release_start': '2026-09-04', 'release_raw': '2026-09-04',
                  'release_end': '2026-09-04', 'release_date_verified_at': '2026-10-03T13:02:27Z',
                  'release_date_normalization': 'steam_release_time_taipei', 'release_date_conflict': False}
        merged = keep_newer_release(existing, master)
        self.assertEqual((merged['release_start'], merged['release_end'], merged['release_raw']),
                         ('2026-09-04',) * 3)
        self.assertEqual(merged['release_store_date'], '2026-09-03')
        self.assertEqual(merged['release_date_normalization'], 'steam_release_time_taipei')
        self.assertIs(merged['release_date_conflict'], False)
        self.assertEqual(merged['release_date_verified_at'], '2026-10-03T13:02:59Z')
        self.assertEqual(merged['twitch_admission'], existing['twitch_admission'])
        self.assertNotIn('release_date_diagnostics', metadata_gaps(merged, source_of(existing)))

    def test_other_kept_rows_are_left_alone(self):
        existing = store_day_admission()
        two_days = {**existing, 'release_start': '2026-09-02', 'release_raw': '2026-09-02', 'release_end': '2026-09-02',
                    'release_store_date': '2026-09-02'}
        merged = keep_newer_release(two_days, fresh_historical(two_days))
        self.assertEqual(merged['release_start'], '2026-09-02')
        self.assertEqual(merged['release_date_normalization'], 'steam_taiwan_store_date_authoritative')
        plain = verified_before_diagnostics()
        self.assertEqual(keep_newer_release(plain, fresh_historical(plain))['release_start'], '2026-09-25')


if __name__ == '__main__':
    unittest.main()
