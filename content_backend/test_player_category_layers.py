"""Player-mode authority, clock boundaries, and composition compatibility."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import inspect
from pathlib import Path
import types
import unittest
from unittest.mock import Mock, patch

import steam_player_modes as legacy
import reconcile_catalog as legacy_reconcile
from radar_backend.adapters import player_categories as runtime
from radar_backend.adapters import catalog_rules
from radar_backend.application.reconciliation import ReconcilePorts, reconcile
from radar_backend.domain import player_categories as rules
from radar_backend.domain.catalog import metadata_gaps
from radar_backend.domain.release import keep_newer_release

NOW = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
CHECKED = "2026-10-09T15:00:00Z"


def evidence(checked=CHECKED, categories=None, source=rules.BROWSE_SOURCE, appid=123):
    return {
        "appid": appid,
        "categories": (
            [{"id": 1, "description": "Multi-player"}]
            if categories is None
            else categories
        ),
        "categories_source": source,
        "categories_checked_at": checked,
    }


def pure_checked(record, clock):
    return rules._category_checked_at(
        record, parse_time=datetime.fromisoformat, now=clock
    )


def pure_verified(record, clock):
    return rules.has_verified_categories(
        record,
        validate_categories=rules.valid_categories,
        category_checked_at=lambda row: pure_checked(row, clock),
        sources=(rules.BROWSE_SOURCE, rules.APPDETAILS_SOURCE),
    )


def pure_preserve(existing, incoming, clock):
    return rules.preserve_player_categories(
        existing,
        incoming,
        verified_categories=lambda row: pure_verified(row, clock),
        category_checked_at=lambda row: pure_checked(row, clock),
    )


def complete_record():
    return {
        **evidence(categories=[]),
        "followers": 6000,
        "release_start": "2030-01-01",
        "release_display_precision": "date_full",
        "header_image": "known.jpg",
        "artwork_checked_at": CHECKED,
        "language_support": {"english": True},
        "tags": [],
        "tags_fetch_status": "ok",
        "tag_labels_language": "zh-TW",
        "genres_fetch_status": "ok",
        "genre_labels_language": "zh-TW",
        "description_checked_at": CHECKED,
        "short_description_language": "zh-TW",
    }


class ClockAndAuthorityTests(unittest.TestCase):
    def test_both_runtime_facades_keep_public_signatures(self):
        expected = {
            "valid_categories": ("value",),
            "_category_checked_at": ("record",),
            "has_verified_categories": ("record",),
            "category_fields": ("item", "details", "appid", "checked_at"),
            "preserve_player_categories": ("existing", "incoming"),
        }
        for module in (legacy, runtime):
            for name, parameters in expected.items():
                with self.subTest(module=module.__name__, name=name):
                    signature = inspect.signature(getattr(module, name))
                    self.assertEqual(tuple(signature.parameters), parameters)
                    self.assertTrue(
                        all(
                            value.default is inspect.Parameter.empty
                            for value in signature.parameters.values()
                        )
                    )

    def test_time_comparison_reads_clock_on_every_comparison(self):
        existing = evidence((NOW + timedelta(minutes=5)).isoformat())
        incoming = evidence((NOW + timedelta(minutes=4)).isoformat(), categories=[])
        clock = Mock(side_effect=[NOW, NOW, NOW, NOW])
        result = pure_preserve(existing, incoming, clock)
        self.assertEqual(
            result,
            {**incoming, **{key: existing[key] for key in rules.CATEGORY_FIELDS}},
        )
        self.assertEqual(clock.call_count, 4)

    def test_boundary_crossing_retains_original_none_comparison_exception(self):
        existing = evidence((NOW + timedelta(minutes=5)).isoformat())
        incoming = evidence((NOW + timedelta(minutes=4)).isoformat(), categories=[])
        clock = Mock(side_effect=[NOW, NOW, NOW, NOW - timedelta(microseconds=1)])
        with self.assertRaises(TypeError):
            pure_preserve(existing, incoming, clock)
        self.assertEqual(clock.call_count, 4)

    def test_mismatched_identity_short_circuits_before_validation_and_clock(self):
        checked = Mock(side_effect=AssertionError("no evidence lookup"))
        verified = Mock(side_effect=AssertionError("no evidence lookup"))
        incoming = evidence(appid=456)
        result = rules.preserve_player_categories(
            evidence(),
            incoming,
            verified_categories=verified,
            category_checked_at=checked,
        )
        self.assertEqual(result, incoming)
        self.assertIsNot(result, incoming)
        checked.assert_not_called()
        verified.assert_not_called()

    def test_incoming_partial_failure_short_circuits_timestamp_comparison(self):
        verified = Mock(side_effect=[True, False])
        checked = Mock(side_effect=AssertionError("comparison should be skipped"))
        existing = evidence()
        self.assertEqual(
            rules.preserve_player_categories(
                existing,
                {},
                verified_categories=verified,
                category_checked_at=checked,
            ),
            {key: existing[key] for key in rules.CATEGORY_FIELDS},
        )
        self.assertEqual(verified.call_args_list[0].args, (existing,))
        checked.assert_not_called()

    def test_ineligible_existing_short_circuits_incoming_evidence(self):
        verified = Mock(return_value=False)
        checked = Mock(side_effect=AssertionError("comparison should be skipped"))
        incoming = evidence()
        self.assertEqual(
            rules.preserve_player_categories(
                {},
                incoming,
                verified_categories=verified,
                category_checked_at=checked,
            ),
            incoming,
        )
        verified.assert_called_once_with({})
        checked.assert_not_called()

    def test_helper_callbacks_run_in_original_order(self):
        trace = []
        existing, incoming = evidence(), evidence(categories=[])

        def verified(row):
            trace.append(("verified", row is existing))
            return True

        def checked(row):
            trace.append(("checked", row is existing))
            return NOW

        self.assertEqual(
            rules.preserve_player_categories(
                existing,
                incoming,
                verified_categories=verified,
                category_checked_at=checked,
            ),
            incoming,
        )
        self.assertEqual(
            trace,
            [
                ("verified", True),
                ("verified", False),
                ("checked", False),
                ("checked", True),
            ],
        )

    def test_facades_read_their_current_clock_on_each_lookup(self):
        for module in (legacy, runtime):
            with self.subTest(module=module.__name__), patch.object(
                module, "datetime"
            ) as clock:
                clock.fromisoformat.side_effect = datetime.fromisoformat
                clock.now.side_effect = [NOW, NOW, NOW, NOW]
                pure_preserve_result = pure_preserve(
                    evidence(), evidence(categories=[]), lambda: NOW
                )
                self.assertEqual(
                    module.preserve_player_categories(
                        evidence(), evidence(categories=[])
                    ),
                    pure_preserve_result,
                )
                self.assertEqual(clock.now.call_count, 4)
                self.assertEqual(clock.fromisoformat.call_count, 4)

    def test_missing_timestamp_does_not_resolve_runtime_parser_or_clock(self):
        for module in (legacy, runtime):
            with self.subTest(module=module.__name__), patch.object(
                module, "datetime", object()
            ):
                self.assertIsNone(module._category_checked_at({}))

    def test_facade_uses_current_validation_and_sources(self):
        for module in (legacy, runtime):
            with self.subTest(module=module.__name__), patch.object(
                module, "valid_categories", return_value=True
            ) as valid, patch.object(
                module, "_category_checked_at", return_value=NOW
            ) as checked, patch.object(
                module, "BROWSE_SOURCE", "test verified source"
            ):
                row = {
                    "categories": "injected validation",
                    "categories_source": "test verified source",
                }
                self.assertTrue(module.has_verified_categories(row))
                valid.assert_called_once_with("injected validation")
                checked.assert_called_once_with(row)

    def test_facade_uses_current_preservation_helpers_and_field_list(self):
        for module in (legacy, runtime):
            with self.subTest(module=module.__name__), patch.object(
                module, "has_verified_categories", side_effect=[True, False]
            ) as verified, patch.object(
                module,
                "_category_checked_at",
                side_effect=AssertionError("short circuit"),
            ), patch.object(
                module, "CATEGORY_FIELDS", ("custom_evidence",)
            ):
                self.assertEqual(
                    module.preserve_player_categories(
                        {"custom_evidence": 1}, {"other": 2}
                    ),
                    {"custom_evidence": 1, "other": 2},
                )
                self.assertEqual(verified.call_count, 2)

    def test_preservation_keeps_shallow_reference_contract(self):
        existing = evidence()
        incoming = evidence(checked="bad", categories=[])
        before = copy.deepcopy((existing, incoming))
        result = pure_preserve(existing, incoming, lambda: NOW)
        self.assertIs(result["categories"], existing["categories"])
        self.assertIsNot(result, existing)
        self.assertIsNot(result, incoming)
        self.assertEqual((existing, incoming), before)

    def test_category_rows_are_new_objects_with_deduplicated_first_description(self):
        details = {
            "steam_appid": 123,
            "type": "game",
            "categories": [
                {"id": 1, "description": "first", "unpublished": True},
                {"id": 1, "description": "later"},
                {"id": 2, "description": "solo"},
            ],
        }
        original = copy.deepcopy(details)
        fields = rules.category_fields({}, details, 123, CHECKED)
        self.assertEqual(
            fields["categories"],
            [{"id": 1, "description": "first"}, {"id": 2, "description": "solo"}],
        )
        self.assertIsNot(fields["categories"][0], details["categories"][0])
        self.assertEqual(details, original)

    def test_canonical_catalog_rules_use_current_runtime_helpers(self):
        with patch.object(
            catalog_rules,
            "preserve_player_categories",
            return_value={"retained": "runtime"},
        ) as preserve:
            self.assertEqual(
                catalog_rules.keep_newer_release({}, {}), {"retained": "runtime"}
            )
            preserve.assert_called_once_with({}, {})
        with patch.object(
            catalog_rules, "keep_newer_release", side_effect=lambda _row, source: source
        ) as keep, patch.object(
            catalog_rules, "has_verified_categories", return_value=True
        ) as verified:
            row = complete_record()
            self.assertEqual(catalog_rules.metadata_gaps(row, row), [])
            keep.assert_called_once_with(row, row)
            verified.assert_called_once_with(row)

    def test_pure_metadata_missing_record_skips_all_rule_ports(self):
        keep = Mock(side_effect=AssertionError("missing record"))
        verified = Mock(side_effect=AssertionError("missing record"))
        self.assertEqual(
            metadata_gaps(
                None, {}, preserve_release=keep, verified_categories=verified
            ),
            ["missing_record"],
        )
        keep.assert_not_called()
        verified.assert_not_called()

    def test_pure_release_preserves_newer_date_without_mutating_inputs(self):
        existing = {
            "release_display_precision": "date_full",
            "release_start": "2030-02-01",
            "release_date_verified_at": "2026-10-09T15:00:00Z",
        }
        incoming = {
            "release_start": "2030-01-01",
            "release_date_verified_at": "2026-10-09T14:00:00Z",
            "release_end": "obsolete",
            "extra": True,
        }
        before = copy.deepcopy((existing, incoming))
        preserve = Mock(side_effect=lambda _old, row: dict(row))
        result = keep_newer_release(existing, incoming, preserve_categories=preserve)
        self.assertEqual(result["release_start"], "2030-02-01")
        self.assertNotIn("release_end", result)
        self.assertTrue(result["extra"])
        preserve.assert_called_once()
        self.assertEqual((existing, incoming), before)

    def test_legacy_metadata_facade_uses_current_helpers(self):
        with patch.object(
            legacy_reconcile,
            "keep_newer_release",
            side_effect=lambda _row, source: source,
        ) as keep, patch.object(
            legacy_reconcile, "has_verified_categories", return_value=True
        ) as verified:
            row = complete_record()
            self.assertEqual(legacy_reconcile.metadata_gaps(row, row), [])
            keep.assert_called_once_with(row, row)
            verified.assert_called_once_with(row)


class ReconciliationClockPortsTests(unittest.TestCase):
    def inputs(self):
        row = complete_record()
        files = {
            Path("virtual/master.json"): {"games": [row]},
            Path("virtual/data/games/123.json"): row,
            Path("virtual/data/index.json"): {"game_count": 1},
        }
        now = Mock(return_value=NOW)
        old_arguments = (
            lambda path, default=None: files.get(path, default),
            lambda path, value: files.update({path: value}),
            lambda _path: set(),
            Mock(side_effect=AssertionError("already complete")),
            Mock(side_effect=AssertionError("already complete")),
            Mock(),
            lambda _path, _row: True,
            Mock(),
            lambda: types.SimpleNamespace(headers={}),
            lambda: 0,
            now,
            lambda: CHECKED,
            OSError,
            RuntimeError,
        )
        return files, now, old_arguments

    def test_original_fourteen_positional_ports_use_injected_clock(self):
        files, now, old_arguments = self.inputs()
        ports = ReconcilePorts(*old_arguments)
        result = reconcile(
            Path("virtual/master.json"), Path("virtual/data"), 0, ports=ports
        )
        self.assertTrue(result["complete"])
        self.assertEqual(result["attempted"], 0)
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(now.call_count, 15)
        self.assertEqual(now.call_args_list[0].args[0].key, "Asia/Taipei")
        self.assertTrue(
            all(call.args == (timezone.utc,) for call in now.call_args_list[1:])
        )
        self.assertEqual(
            files[Path("virtual/data/content_refresh_status.json")], result
        )

    def test_explicit_rule_ports_skip_fallback_clock(self):
        _, now, old_arguments = self.inputs()
        preserve = Mock(side_effect=lambda _old, incoming: incoming)
        gaps = Mock(return_value=[])
        verified = Mock(side_effect=AssertionError("no cache evaluation"))
        ports = ReconcilePorts(
            *old_arguments,
            preserve_release=preserve,
            metadata_gaps=gaps,
            verified_categories=verified,
        )
        result = reconcile(
            Path("virtual/master.json"), Path("virtual/data"), 0, ports=ports
        )
        self.assertTrue(result["complete"])
        self.assertEqual(now.call_count, 1)
        self.assertEqual(preserve.call_count, 1)
        self.assertEqual(gaps.call_count, 2)
        verified.assert_not_called()

    def test_legacy_composition_passes_current_rule_ports(self):
        with patch.object(
            legacy_reconcile, "_reconcile", return_value={"captured": True}
        ) as usecase, patch.object(
            legacy_reconcile, "keep_newer_release"
        ) as keep, patch.object(
            legacy_reconcile, "metadata_gaps"
        ) as gaps, patch.object(
            legacy_reconcile, "has_verified_categories"
        ) as verified:
            result = legacy_reconcile.reconcile(Path("master.json"), Path("data"), 5)
            self.assertEqual(result, {"captured": True})
            ports = usecase.call_args.kwargs["ports"]
            self.assertIs(ports.preserve_release, keep)
            self.assertIs(ports.metadata_gaps, gaps)
            self.assertIs(ports.verified_categories, verified)
            self.assertIs(ports.exists, Path.exists)


class HistoricalExistencePortTests(unittest.TestCase):
    def run_case(
        self,
        *,
        day="2020-01-01",
        existing=True,
        record=None,
        precision="month",
        error=None,
    ):
        source = {
            **complete_record(),
            "release_start": day,
            "release_display_precision": precision,
        }
        files = {
            Path("virtual/master.json"): {"games": [source]},
            Path("virtual/data/games/123.json"): record,
            Path("virtual/data/index.json"): {"game_count": 0},
        }
        trace = []

        def read(path, default=None):
            trace.append(("read", path))
            return files.get(path, default)

        def exists(path):
            trace.append(("exists", path))
            if error:
                raise error
            return existing

        exists_mock = Mock(side_effect=exists)
        ports = ReconcilePorts(
            read,
            lambda _path, _value: None,
            lambda _path: set(),
            Mock(side_effect=AssertionError("bounded at zero")),
            Mock(),
            Mock(),
            lambda _path, _row: True,
            Mock(),
            lambda: types.SimpleNamespace(headers={}),
            lambda: 0,
            lambda _tz: NOW,
            lambda: CHECKED,
            OSError,
            RuntimeError,
            preserve_release=lambda _old, incoming: incoming,
            metadata_gaps=lambda _row, _source: [],
            verified_categories=lambda _row: True,
            exists=exists_mock,
        )
        result = reconcile(
            Path("virtual/master.json"), Path("virtual/data"), 0, ports=ports
        )
        return result, exists_mock, trace

    def test_existing_historical_file_accepts_month_precision(self):
        result, exists, trace = self.run_case(record=complete_record())
        self.assertEqual(result["qualified_master"], 1)
        exists.assert_called_once_with(Path("virtual/data/games/123.json"))
        self.assertLess(
            trace.index(("exists", Path("virtual/data/games/123.json"))),
            trace.index(("read", Path("virtual/data/games/123.json"))),
        )

    def test_existing_empty_json_is_still_historical(self):
        result, exists, _ = self.run_case(record={})
        self.assertEqual(result["qualified_master"], 1)
        exists.assert_called_once()

    def test_existing_null_json_is_still_historical(self):
        result, exists, _ = self.run_case(record=None)
        self.assertEqual(result["qualified_master"], 1)
        exists.assert_called_once()

    def test_missing_file_rejects_inexact_historical_source(self):
        result, exists, trace = self.run_case(existing=False)
        self.assertEqual(result["qualified_master"], 0)
        exists.assert_called_once()
        self.assertNotIn(("read", Path("virtual/data/games/123.json")), trace)

    def test_missing_historical_file_can_still_accept_exact_date(self):
        result, exists, _ = self.run_case(existing=False, precision="date_full")
        self.assertEqual(result["qualified_master"], 1)
        exists.assert_called_once()

    def test_future_source_skips_existence_lookup(self):
        result, exists, _ = self.run_case(day="2030-01-01")
        self.assertEqual(result["qualified_master"], 0)
        exists.assert_not_called()

    def test_invalid_date_skips_existence_lookup(self):
        result, exists, _ = self.run_case(day="unknown")
        self.assertEqual(result["qualified_master"], 0)
        exists.assert_not_called()

    def test_today_source_skips_existence_lookup(self):
        result, exists, _ = self.run_case(day="2026-10-09")
        self.assertEqual(result["qualified_master"], 0)
        exists.assert_not_called()

    def test_permission_failure_propagates_before_reading_record(self):
        with self.assertRaisesRegex(PermissionError, "denied"):
            self.run_case(error=PermissionError("denied"))


CATEGORY_CASES = [
    ("empty", [], True),
    ("one", [{"id": 1, "description": "Multiplayer"}], True),
    ("empty_description", [{"id": 2, "description": ""}], True),
    ("extra_fields", [{"id": 9, "description": "Co-op", "other": True}], True),
    ("duplicate", [{"id": 1, "description": "a"}, {"id": 1, "description": "b"}], True),
    ("none", None, False),
    ("tuple", (), False),
    ("mapping", {}, False),
    ("string", "Multiplayer", False),
    ("string_row", ["1"], False),
    ("bool_id", [{"id": True, "description": "x"}], False),
    ("zero_id", [{"id": 0, "description": "x"}], False),
    ("negative_id", [{"id": -1, "description": "x"}], False),
    ("float_id", [{"id": 1.0, "description": "x"}], False),
    ("str_id", [{"id": "1", "description": "x"}], False),
    ("missing_id", [{"description": "x"}], False),
    ("missing_description", [{"id": 1}], False),
    ("none_description", [{"id": 1, "description": None}], False),
]


def category_case(value, expected):
    def test(self):
        for module in (rules, runtime, legacy):
            with self.subTest(module=module.__name__):
                self.assertIs(module.valid_categories(value), expected)

    return test


for name, value, expected in CATEGORY_CASES:
    setattr(
        ClockAndAuthorityTests,
        "test_category_shape_" + name,
        category_case(value, expected),
    )

TIME_CASES = [
    ("past", (NOW - timedelta(days=365)).isoformat(), True, 1),
    ("exact_now", CHECKED, True, 1),
    (
        "future_inside",
        (NOW + timedelta(minutes=5) - timedelta(microseconds=1)).isoformat(),
        True,
        1,
    ),
    ("future_boundary", (NOW + timedelta(minutes=5)).isoformat(), True, 1),
    (
        "future_outside",
        (NOW + timedelta(minutes=5, microseconds=1)).isoformat(),
        False,
        1,
    ),
    ("offset_equal", "2026-10-09T23:05:00+08:00", True, 1),
    ("offset_outside", "2026-10-09T23:05:00.000001+08:00", False, 1),
    ("naive", "2026-10-09T15:00:00", False, 0),
    ("date_only", "2026-10-09", False, 0),
    ("invalid", "unknown", False, 0),
    ("empty", "", False, 0),
    ("none", None, False, 0),
    ("integer", 1791558000, False, 0),
    ("datetime_value", NOW, False, 0),
]


def time_case(value, expected, calls, source):
    def test(self):
        clock = Mock(return_value=NOW)
        self.assertIs(pure_verified(evidence(value, source=source), clock), expected)
        self.assertEqual(clock.call_count, calls)
        for module in (legacy, runtime):
            with self.subTest(module=module.__name__), patch.object(
                module, "datetime"
            ) as patched:
                patched.fromisoformat.side_effect = datetime.fromisoformat
                patched.now.return_value = NOW
                self.assertIs(
                    module.has_verified_categories(evidence(value, source=source)),
                    expected,
                )
                self.assertEqual(patched.now.call_count, calls)

    return test


for source_name, source in [
    ("browse", rules.BROWSE_SOURCE),
    ("appdetails", rules.APPDETAILS_SOURCE),
]:
    for name, value, expected, calls in TIME_CASES:
        setattr(
            ClockAndAuthorityTests,
            "test_authority_" + source_name + "_" + name,
            time_case(value, expected, calls, source),
        )


SOURCE_CASES = [
    ("none", None),
    ("community", "community tags"),
    ("blank", ""),
    ("integer", 1),
]


def source_case(source):
    def test(self):
        clock = Mock(
            side_effect=AssertionError("untrusted evidence must not read clock")
        )
        self.assertFalse(pure_verified(evidence(source=source), clock))
        clock.assert_not_called()

    return test


for name, source in SOURCE_CASES:
    setattr(ClockAndAuthorityTests, "test_source_" + name, source_case(source))


if __name__ == "__main__":
    unittest.main()
