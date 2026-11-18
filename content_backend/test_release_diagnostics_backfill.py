"""A record verified before the timestamp diagnostics existed must be able to complete.

Dragon Shelter (2712590) stayed in pending_enrichment from 11-15: its kept,
older date evidence won every merge and also dropped the freshly computed
diagnostics, so release_date_diagnostics could never clear.
"""
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


if __name__ == '__main__':
    unittest.main()
