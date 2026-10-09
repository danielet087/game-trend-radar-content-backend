"""Browser projection contracts across pure rules, storage, and both APIs."""

import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import public_catalog as legacy
from radar_core.domain.twitch_admission import preserve_twitch_admission
from radar_backend.adapters import catalog_projection as canonical
from radar_backend.domain import catalog_projection as rules
from radar_backend.domain.release import RELEASE_FIELDS
from radar_backend.state import catalog_projection as storage


class ProjectionRulesTests(unittest.TestCase):
    def test_revision_hashes_hidden_metadata_too(self):
        rows = [{"appid": 7, "name": "遊戲", "internal": {"audit": 1}}]
        other = [{**rows[0], "internal": {"audit": 2}}]
        self.assertNotEqual(rules.catalog_revision(rows), rules.catalog_revision(other))
        self.assertEqual(
            rules.catalog_payload(rows, "first", "a")["games"],
            rules.catalog_payload(other, "second", "b")["games"],
        )

    def test_hash_canonicalizes_nested_dictionary_key_order(self):
        first = [
            {
                "name": "遊戲",
                "appid": 7,
                "language_support": {"english": True, "tchinese": False},
            }
        ]
        second = [
            {
                "language_support": {"tchinese": False, "english": True},
                "appid": 7,
                "name": "遊戲",
            }
        ]
        self.assertEqual(rules.catalog_revision(first), rules.catalog_revision(second))

    def test_hash_keeps_row_order(self):
        rows = [{"appid": 1}, {"appid": 2}]
        self.assertNotEqual(
            rules.catalog_revision(rows), rules.catalog_revision(rows[::-1])
        )

    def test_hash_keeps_nested_list_order(self):
        self.assertNotEqual(
            rules.catalog_revision([{"appid": 1, "tags": ["Action", "策略"]}]),
            rules.catalog_revision([{"appid": 1, "tags": ["策略", "Action"]}]),
        )

    def test_revision_uses_utf8_compact_full_rows_and_twenty_hex_digits(self):
        rows = [{"name": "遊戲☃", "appid": 7}]
        expected = hashlib.sha256(
            '[{"appid":7,"name":"遊戲☃"}]'.encode("utf-8")
        ).hexdigest()[:20]
        self.assertEqual(rules.catalog_revision(rows), expected)
        self.assertEqual(len(expected), 20)

    def test_hash_does_not_tighten_legacy_nonfinite_serialization(self):
        rows = [{"appid": 1, "diagnostic": float("inf")}]
        expected = hashlib.sha256(b'[{"appid":1,"diagnostic":Infinity}]').hexdigest()[
            :20
        ]
        self.assertEqual(rules.catalog_revision(rows), expected)

    def test_projection_uses_field_order_instead_of_source_key_order(self):
        row = {field: index for index, field in enumerate(reversed(rules.FIELDS))}
        projected = rules.catalog_payload([row], "clock", "revision")["games"][0]
        self.assertEqual(tuple(projected), rules.FIELDS)

    def test_projection_excludes_nonbrowser_fields(self):
        payload = rules.catalog_payload(
            [{"appid": 1, "name": "One", "private": "hidden"}], "t", "r"
        )
        self.assertEqual(payload["games"], [{"appid": 1, "name": "One"}])

    def test_missing_fields_are_absent_and_explicit_nulls_are_preserved(self):
        payload = rules.catalog_payload([{"appid": 1, "name": None}], "t", "r")
        self.assertEqual(payload["games"], [{"appid": 1, "name": None}])
        self.assertNotIn("name_en", payload["games"][0])

    def test_payload_preserves_v3_count_and_envelope_order(self):
        payload = rules.catalog_payload([{}, {}], "clock", "revision")
        self.assertEqual(
            tuple(payload), ("version", "revision", "generated_at", "count", "games")
        )
        self.assertEqual(
            payload,
            {
                "version": 3,
                "revision": "revision",
                "generated_at": "clock",
                "count": 2,
                "games": [{}, {}],
            },
        )

    def test_projection_keeps_nested_references_in_fresh_row_dictionaries(self):
        row = {"appid": 1, "tags": ["Action"], "language_support": {"english": True}}
        projected = rules.catalog_payload([row], "t", "r")["games"][0]
        self.assertIsNot(projected, row)
        self.assertIs(projected["tags"], row["tags"])
        self.assertIs(projected["language_support"], row["language_support"])

    def test_pure_projection_and_revision_leave_inputs_unchanged(self):
        rows = [{"appid": 1, "tags": ["Action"], "private": {"seen": [1]}}]
        before = copy.deepcopy(rows)
        revision = rules.catalog_revision(rows)
        rules.catalog_payload(rows, "t", revision)
        self.assertEqual(rows, before)

    def test_revision_codec_and_digest_are_explicit_ports(self):
        codec = SimpleNamespace(dumps=Mock(return_value="raw-json"))
        digest = SimpleNamespace(
            sha256=Mock(return_value=SimpleNamespace(hexdigest=lambda: "f" * 64))
        )
        self.assertEqual(
            rules.catalog_revision([], json_codec=codec, hash_codec=digest), "f" * 20
        )
        codec.dumps.assert_called_once_with(
            [], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        digest.sha256.assert_called_once_with(b"raw-json")


class ProjectionStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "nested" / "data"

    def write(self, rows, generated_at="clock"):
        return storage.write_catalog_projection(
            self.data,
            rows,
            generated_at,
            fields=rules.FIELDS,
            json_codec=json,
            hash_codec=hashlib,
        )

    def test_empty_projection_creates_parents_and_fixed_filename(self):
        receipt = self.write([])
        self.assertEqual(
            receipt,
            {
                "catalog_path": "catalog.json",
                "catalog_revision": hashlib.sha256(b"[]").hexdigest()[:20],
            },
        )
        self.assertEqual([path.name for path in self.data.iterdir()], ["catalog.json"])

    def test_serialized_bytes_use_compact_unicode_and_single_trailing_newline(self):
        self.write([{"name": "遊戲", "appid": 7, "private": True}], "時間")
        revision = hashlib.sha256(
            '[{"appid":7,"name":"遊戲","private":true}]'.encode()
        ).hexdigest()[:20]
        expected = (
            '{"version":3,"revision":"'
            + revision
            + '","generated_at":"時間","count":1,"games":[{"appid":7,"name":"遊戲"}]}\n'
        )
        self.assertEqual(
            (self.data / "catalog.json").read_bytes(), expected.encode("utf-8")
        )

    def test_same_revision_preserves_bytes_mtime_and_prior_generated_at(self):
        rows = [{"appid": 7, "name": "遊戲"}]
        first = self.write(rows, "first")
        path = self.data / "catalog.json"
        os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        before = path.read_bytes(), path.stat().st_mtime_ns
        second = self.write(rows, "second")
        self.assertEqual(first, second)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(json.loads(path.read_text())["generated_at"], "first")

    def test_same_revision_returns_before_mkdir_or_write(self):
        rows = [{"appid": 7}]
        self.write(rows)
        with patch.object(
            Path, "mkdir", side_effect=AssertionError("mkdir on no-op")
        ), patch.object(
            Path, "write_text", side_effect=AssertionError("write on no-op")
        ):
            self.assertEqual(
                self.write(rows, "later")["catalog_revision"],
                rules.catalog_revision(rows),
            )

    def test_matching_revision_alone_repairs_inconsistent_browser_payload(self):
        rows = [{"appid": 7}]
        self.data.mkdir(parents=True)
        path = self.data / "catalog.json"
        original = json.dumps(
            {"revision": rules.catalog_revision(rows), "games": "incorrect"}
        )
        path.write_text(original)
        self.write(rows)
        self.assertEqual(
            json.loads(path.read_text()),
            rules.catalog_payload(rows, "clock", rules.catalog_revision(rows)),
        )

    def test_hidden_metadata_change_rewrites_same_projected_rows(self):
        first = self.write([{"appid": 7, "private": 1}], "first")
        second = self.write([{"appid": 7, "private": 2}], "second")
        self.assertNotEqual(first, second)
        self.assertEqual(
            json.loads((self.data / "catalog.json").read_text())["games"],
            [{"appid": 7}],
        )
        self.assertEqual(
            json.loads((self.data / "catalog.json").read_text())["generated_at"],
            "second",
        )

    def test_equivalent_dictionary_order_keeps_existing_file(self):
        self.write([{"appid": 7, "name": "One"}], "first")
        path = self.data / "catalog.json"
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.write([{"name": "One", "appid": 7}], "second")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_source_row_order_change_rewrites_projection(self):
        first = self.write([{"appid": 1}, {"appid": 2}])
        second = self.write([{"appid": 2}, {"appid": 1}])
        self.assertNotEqual(first, second)
        self.assertEqual(
            json.loads((self.data / "catalog.json").read_text())["games"],
            [{"appid": 2}, {"appid": 1}],
        )

    def test_invalid_json_is_replaced(self):
        self.data.mkdir(parents=True)
        (self.data / "catalog.json").write_text("{broken")
        self.write([{"appid": 7}])
        self.assertEqual(
            json.loads((self.data / "catalog.json").read_text())["count"], 1
        )

    def test_invalid_utf8_is_replaced(self):
        self.data.mkdir(parents=True)
        (self.data / "catalog.json").write_bytes(b"\xff")
        self.write([])
        self.assertEqual(
            json.loads((self.data / "catalog.json").read_text())["count"], 0
        )

    def test_nonobject_documents_keep_original_attribute_error(self):
        self.data.mkdir(parents=True)
        path = self.data / "catalog.json"
        for original in ("null", "[]", "7", '"text"', "true"):
            with self.subTest(original=original):
                path.write_text(original)
                with self.assertRaises(AttributeError):
                    self.write([])
                self.assertEqual(path.read_text(), original)

    def test_read_oserror_is_tolerated(self):
        self.write([])
        with patch.object(Path, "read_text", side_effect=OSError("read failed")):
            self.write([{"appid": 7}])
        self.assertEqual(
            json.loads((self.data / "catalog.json").read_text())["count"], 1
        )

    def test_read_typeerror_is_tolerated(self):
        self.write([])
        with patch.object(Path, "read_text", side_effect=TypeError("reader failed")):
            self.write([{"appid": 7}])
        self.assertEqual(
            json.loads((self.data / "catalog.json").read_text())["count"], 1
        )

    def test_exists_failure_propagates(self):
        with patch.object(Path, "exists", side_effect=OSError("exists failed")):
            with self.assertRaisesRegex(OSError, "exists failed"):
                self.write([])

    def test_mkdir_failure_propagates(self):
        with patch.object(Path, "mkdir", side_effect=OSError("mkdir failed")):
            with self.assertRaisesRegex(OSError, "mkdir failed"):
                self.write([])

    def test_write_failure_propagates(self):
        with patch.object(Path, "write_text", side_effect=OSError("write failed")):
            with self.assertRaisesRegex(OSError, "write failed"):
                self.write([])

    def test_unserializable_rows_fail_before_creating_directories(self):
        with self.assertRaises(TypeError):
            self.write([{"appid": 7, "private": object()}])
        self.assertFalse(self.data.exists())

    def test_hash_failure_precedes_storage(self):
        digest = SimpleNamespace(sha256=Mock(side_effect=RuntimeError("hash failed")))
        with patch.object(
            Path, "exists", side_effect=AssertionError("storage called early")
        ):
            with self.assertRaisesRegex(RuntimeError, "hash failed"):
                storage.write_catalog_projection(
                    self.data,
                    [],
                    "t",
                    fields=rules.FIELDS,
                    json_codec=json,
                    hash_codec=digest,
                )
        self.assertFalse(self.data.exists())


class ProjectionCompatibilityTests(unittest.TestCase):
    def test_canonical_and_legacy_apis_write_identical_bytes_and_receipts(self):
        rows = [
            {"appid": 1, "name": "遊戲", "tags": ["Action"], "private": {"audit": 2}}
        ]
        before = copy.deepcopy(rows)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_receipt = legacy.write_catalog_projection(root / "old", rows, "fixed")
            new_receipt = canonical.write_catalog_projection(
                root / "new", rows, "fixed"
            )
            self.assertEqual(old_receipt, new_receipt)
            self.assertEqual(
                (root / "old/catalog.json").read_bytes(),
                (root / "new/catalog.json").read_bytes(),
            )
        self.assertEqual(rows, before)

    def test_public_signature_keeps_existing_three_positional_parameters(self):
        for api in (legacy, canonical):
            with self.subTest(api=api.__name__):
                signature = inspect.signature(api.write_catalog_projection)
                self.assertEqual(
                    tuple(signature.parameters), ("data_dir", "rows", "generated_at")
                )
                self.assertTrue(
                    all(
                        value.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
                        for value in signature.parameters.values()
                    )
                )
                self.assertEqual(signature.return_annotation, dict)
                self.assertIs(signature.parameters["data_dir"].annotation, Path)

    def test_fields_are_reexported_from_the_single_domain_owner(self):
        self.assertIs(legacy.FIELDS, rules.FIELDS)
        self.assertIs(canonical.FIELDS, rules.FIELDS)

    def test_legacy_core_helper_and_release_fields_keep_identity(self):
        self.assertIs(legacy.preserve_twitch_admission, preserve_twitch_admission)
        self.assertIs(legacy.RELEASE_FIELDS, RELEASE_FIELDS)

    def test_each_facade_reads_its_current_fields_global(self):
        for api in (legacy, canonical):
            with self.subTest(api=api.__name__), tempfile.TemporaryDirectory() as tmp:
                with patch.object(api, "FIELDS", ("name", "appid")):
                    api.write_catalog_projection(
                        Path(tmp),
                        [{"appid": 7, "name": "遊戲", "tags": ["Action"]}],
                        "t",
                    )
                result = json.loads((Path(tmp) / "catalog.json").read_text())
                self.assertEqual(tuple(result["games"][0]), ("name", "appid"))
                self.assertNotIn("tags", result["games"][0])

    def test_each_facade_reads_its_current_json_codec_global(self):
        for api in (legacy, canonical):
            with self.subTest(api=api.__name__), tempfile.TemporaryDirectory() as tmp:
                codec = SimpleNamespace(
                    dumps=Mock(wraps=json.dumps), loads=Mock(wraps=json.loads)
                )
                data = Path(tmp)
                with patch.object(api, "json", codec):
                    api.write_catalog_projection(data, [{"appid": 7}], "first")
                    api.write_catalog_projection(data, [{"appid": 7}], "second")
                self.assertEqual(codec.dumps.call_count, 3)
                self.assertEqual(codec.loads.call_count, 1)
                self.assertEqual(
                    codec.dumps.call_args_list[0].kwargs,
                    {
                        "ensure_ascii": False,
                        "sort_keys": True,
                        "separators": (",", ":"),
                    },
                )
                self.assertEqual(
                    codec.dumps.call_args_list[1].kwargs,
                    {"ensure_ascii": False, "separators": (",", ":")},
                )

    def test_each_facade_reads_its_current_hash_global(self):
        for api in (legacy, canonical):
            with self.subTest(api=api.__name__), tempfile.TemporaryDirectory() as tmp:
                digest = SimpleNamespace(
                    sha256=Mock(
                        return_value=SimpleNamespace(hexdigest=lambda: "a" * 64)
                    )
                )
                with patch.object(api, "hashlib", digest):
                    receipt = api.write_catalog_projection(
                        Path(tmp), [{"appid": 7}], "t"
                    )
                self.assertEqual(receipt["catalog_revision"], "a" * 20)
                digest.sha256.assert_called_once_with(b'[{"appid":7}]')

    def test_legacy_release_wrapper_uses_current_preservation_callback(self):
        existing, incoming = {"appid": 7}, {"appid": 7, "name": "Incoming"}
        callback = Mock(return_value={"appid": 7, "name": "Injected"})
        with patch.object(legacy, "preserve_player_categories", callback):
            result = legacy.keep_newer_release(existing, incoming)
        callback.assert_called_once_with(existing, incoming)
        self.assertEqual(result, {"appid": 7, "name": "Injected"})

    def test_legacy_imported_helpers_remain_available(self):
        for name in (
            "hashlib",
            "json",
            "datetime",
            "timezone",
            "Path",
            "preserve_player_categories",
            "preserve_twitch_admission",
            "RELEASE_FIELDS",
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(legacy, name))


if __name__ == "__main__":
    unittest.main()
