"""Description boundaries: lazy storage, version evidence, and compatibility ports."""

import hashlib
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from radar_backend.application import localized_descriptions as application
from radar_backend.domain import localized_descriptions as domain
from radar_backend.state import description_translations as state


MODULES = (
    "localized_descriptions",
    "radar_backend.adapters.localized_descriptions",
)
DESCRIPTION_KEYS = (
    "short_description",
    "short_description_en",
    "short_description_language",
    "short_description_source",
)


class DescriptionDomainTests(unittest.TestCase):
    def test_cleaning_does_not_turn_non_text_into_a_description(self):
        for value in (None, False, 0, 100, [], {}, object()):
            with self.subTest(value=value):
                self.assertEqual(domain.clean_description(value), "")

    def test_html_entity_decoding_occurs_after_tag_removal(self):
        self.assertEqual(
            domain.clean_description(
                " <b>探索\n世界</b>&nbsp;&amp; &lt;b&gt;冒險&lt;/b&gt; "
            ),
            "探索 世界 & <b>冒險</b>",
        )

    def test_han_threshold_is_four_and_accepts_extension_a(self):
        self.assertFalse(domain.is_chinese("探索世"))
        self.assertTrue(domain.is_chinese("探索世界"))
        self.assertTrue(domain.is_chinese("\u3400\u3401\u3402\u3403"))
        self.assertFalse(domain.is_chinese("\u3400\u3401\u3402"))

    def test_kana_is_rejected_but_middle_dot_is_accepted(self):
        for kana in ("ぁ", "ゖ", "ァ", "ヺ"):
            with self.subTest(kana=kana):
                self.assertFalse(domain.is_chinese("探索世界" + kana))
        self.assertTrue(domain.is_chinese("探索世界・冒險"))

    def test_fingerprint_hashes_clean_english_as_exact_utf8(self):
        self.assertEqual(
            domain.description_fingerprint("Café 世界"),
            hashlib.sha256("Café 世界".encode("utf-8")).hexdigest(),
        )
        self.assertNotEqual(
            domain.description_fingerprint("English"),
            domain.description_fingerprint("English "),
        )

    def test_output_fields_are_atomic_even_when_converter_returns_empty(self):
        self.assertEqual(
            domain.localized_fields("English", "", "steam_tchinese"),
            dict(zip(DESCRIPTION_KEYS, ("", "English", "", "steam_tchinese"))),
        )

    def test_unavailable_clears_all_description_fields(self):
        existing = dict(
            zip(DESCRIPTION_KEYS, ("探索世界", "Old", "zh-TW", "steam_tchinese"))
        )
        existing["name"] = "Original"
        incoming = {"short_description_source": "unavailable", "name": "Updated"}
        result = domain.merge_description_fields(existing, incoming)
        self.assertEqual(result["name"], "Updated")
        self.assertEqual(
            [result[key] for key in DESCRIPTION_KEYS], ["", "", "", "unavailable"]
        )
        self.assertEqual(existing["short_description"], "探索世界")

    def test_explicit_source_copies_none_and_empty_values_without_coercion(self):
        incoming = {
            "short_description_source": None,
            "short_description": None,
            "short_description_en": "",
            "short_description_language": None,
        }
        self.assertEqual(domain.merge_description_fields({}, incoming), incoming)

    def test_legacy_update_keeps_existing_checked_locale_atomically(self):
        existing = dict(
            zip(DESCRIPTION_KEYS, ("探索世界", "Old", "zh-TW", "steam_tchinese"))
        )
        incoming = {
            "short_description": "English again",
            "short_description_en": "New",
            "name": "Updated",
        }
        result = domain.merge_description_fields(existing, incoming)
        for key in DESCRIPTION_KEYS:
            self.assertEqual(result[key], existing[key])
        self.assertEqual(result["name"], "Updated")

    def test_unchecked_locale_accepts_nonempty_legacy_description(self):
        self.assertEqual(
            domain.merge_description_fields(
                {"short_description": "Old", "name": "Name", "count": 1},
                {"short_description": "New", "name": "", "count": 0, "removed": None},
            ),
            {"short_description": "New", "name": "Name", "count": 0},
        )

    def test_merge_preserves_nested_references_and_does_not_mutate_inputs(self):
        nested = {"languages": ["tchinese"]}
        existing = {"nested": nested}
        incoming = {"short_description_source": "unavailable"}
        result = domain.merge_description_fields(existing, incoming)
        self.assertIs(result["nested"], nested)
        self.assertIsNot(result, existing)
        self.assertEqual(existing, {"nested": nested})
        self.assertEqual(incoming, {"short_description_source": "unavailable"})


class DescriptionApplicationTests(unittest.TestCase):
    def invoke(self, values, *, document=None, error=None, convert=lambda value: value):
        calls = []

        def clean(value):
            calls.append(("clean", value))
            return domain.clean_description(value)

        def chinese(value):
            calls.append(("chinese", value))
            return domain.is_chinese(value)

        def translations():
            calls.append(("translations",))
            if error is not None:
                raise error
            return document if document is not None else {}

        def fingerprint(value):
            calls.append(("fingerprint", value))
            return domain.description_fingerprint(value)

        def conversion(value):
            calls.append(("convert", value))
            return convert(value)

        result = application.description_fields(
            123,
            *values,
            clean_description=clean,
            is_chinese=chinese,
            convert=conversion,
            translations=translations,
            fingerprint=fingerprint,
        )
        return result, calls

    def test_official_traditional_does_not_read_missing_editorial_file(self):
        result, calls = self.invoke(
            ("English", "探索世界", "探索广阔世界"), error=FileNotFoundError()
        )
        self.assertEqual(result["short_description_source"], "steam_tchinese")
        self.assertEqual(
            calls,
            [
                ("clean", "English"),
                ("clean", "探索世界"),
                ("clean", "探索广阔世界"),
                ("chinese", "探索世界"),
                ("convert", "探索世界"),
            ],
        )

    def test_official_simplified_does_not_read_missing_editorial_file(self):
        result, calls = self.invoke(
            ("English", "Japanese", "探索广阔世界"), error=FileNotFoundError()
        )
        self.assertEqual(result["short_description_source"], "steam_schinese_converted")
        self.assertEqual(
            calls[-3:],
            [
                ("chinese", "Japanese"),
                ("chinese", "探索广阔世界"),
                ("convert", "探索广阔世界"),
            ],
        )

    def test_editorial_translation_requires_exact_clean_english_version(self):
        document = {
            "123": {
                "source_sha256": domain.description_fingerprint("Explore & build"),
                "text": "探索世界",
            }
        }
        result, calls = self.invoke(
            (" <b>Explore</b> &amp; build ", "English", None), document=document
        )
        self.assertEqual(result["short_description_en"], "Explore & build")
        self.assertEqual(result["short_description_source"], "editorial_zh_tw")
        self.assertEqual(
            calls[-4:],
            [
                ("translations",),
                ("fingerprint", "Explore & build"),
                ("chinese", "探索世界"),
                ("convert", "探索世界"),
            ],
        )

    def test_english_source_change_invalidates_editorial_translation(self):
        document = {
            "123": {
                "source_sha256": domain.description_fingerprint("Old English"),
                "text": "探索世界",
            }
        }
        result, calls = self.invoke(("New English", None, None), document=document)
        self.assertEqual(result["short_description_source"], "unavailable")
        self.assertEqual(
            calls[-2:], [("translations",), ("fingerprint", "New English")]
        )

    def test_empty_english_still_loads_document_and_computes_fingerprint(self):
        document = {
            "123": {
                "source_sha256": domain.description_fingerprint(""),
                "text": "探索世界",
            }
        }
        result, calls = self.invoke((None, None, None), document=document)
        self.assertEqual(result["short_description_source"], "unavailable")
        self.assertEqual(calls[-2:], [("translations",), ("fingerprint", "")])

    def test_translation_lookup_precedes_hash_and_keeps_lookup_errors(self):
        events = []

        class BadDocument:
            def get(self, key, default):
                events.append(("get", key, default))
                raise LookupError("original translation lookup")

        with self.assertRaisesRegex(LookupError, "original translation lookup"):
            application.description_fields(
                123,
                "English",
                None,
                None,
                clean_description=domain.clean_description,
                is_chinese=domain.is_chinese,
                convert=lambda value: events.append(("convert", value)),
                translations=lambda: BadDocument(),
                fingerprint=lambda value: events.append(("hash", value)),
            )
        self.assertEqual(events, [("get", "123", {})])

    def test_storage_errors_escape_without_fallback_or_conversion(self):
        with self.assertRaisesRegex(OSError, "missing translation document"):
            self.invoke(
                ("English", None, None), error=OSError("missing translation document")
            )

    def test_converter_empty_result_retains_official_source_and_empty_language(self):
        result, calls = self.invoke(
            ("English", "探索世界", None), convert=lambda value: ""
        )
        self.assertEqual(result["short_description_source"], "steam_tchinese")
        self.assertEqual(result["short_description_language"], "")
        self.assertNotIn(("translations",), calls)


class DescriptionStorageTests(unittest.TestCase):
    def test_storage_returns_document_reference_without_normalizing_entries(self):
        translations = {"123": None}
        document = {"translations": translations}
        path = SimpleNamespace(read_text=lambda **kwargs: "raw document")
        self.assertIs(state.read_translations(lambda raw: document, path), translations)

    def test_storage_uses_utf8_and_preserves_parse_and_shape_errors(self):
        calls = []
        path = SimpleNamespace(
            read_text=lambda **kwargs: calls.append(kwargs) or "not json"
        )
        with self.assertRaises(json.JSONDecodeError):
            state.load_translations(path)
        self.assertEqual(calls, [{"encoding": "utf-8"}])
        for value, error in (({}, KeyError), (None, TypeError), ([], TypeError)):
            with self.subTest(value=value), self.assertRaises(error):
                state.read_translations(lambda raw: value, path)


class DescriptionFacadeTests(unittest.TestCase):
    def setUp(self):
        self.modules = [importlib.import_module(name) for name in MODULES]
        for module in self.modules:
            module.translations.cache_clear()

    def tearDown(self):
        for module in self.modules:
            module.translations.cache_clear()

    def test_real_editorial_path_is_the_content_backend_document(self):
        expected_path = Path(__file__).with_name("descriptions_zh_tw.json")
        expected = json.loads(expected_path.read_text(encoding="utf-8"))["translations"]
        for module in self.modules:
            with self.subTest(module=module.__name__):
                self.assertEqual(module.translations(), expected)
                self.assertEqual(module.CONVERTER.convert("鼠标和硬盘"), "滑鼠和硬碟")

    def test_cache_preserves_mutable_reference_until_clear(self):
        for module in self.modules:
            with self.subTest(module=module.__name__):
                calls = []
                value = {"123": {"text": "探索世界"}}
                with patch.object(
                    module._state,
                    "read_translations",
                    side_effect=lambda *args: calls.append(args) or value,
                ):
                    first = module.translations()
                    first["added"] = {}
                    second = module.translations()
                    self.assertIs(first, second)
                    self.assertIn("added", second)
                    self.assertEqual(module.translations.cache_info().hits, 1)
                    self.assertEqual(module.translations.cache_info().misses, 1)
                    self.assertEqual(module.translations.cache_info().maxsize, 1)
                    self.assertEqual(len(calls), 1)
                    module.translations.cache_clear()
                    module.translations()
                    self.assertEqual(len(calls), 2)

    def test_read_failures_are_not_cached_and_successful_retry_is_cached(self):
        for module in self.modules:
            with self.subTest(module=module.__name__):
                with patch.object(
                    module._state,
                    "read_translations",
                    side_effect=[OSError("unavailable"), {"123": {}}],
                ) as reader:
                    with self.assertRaisesRegex(OSError, "unavailable"):
                        module.translations()
                    result = module.translations()
                    self.assertIs(module.translations(), result)
                    self.assertEqual(reader.call_count, 2)
                    self.assertEqual(module.translations.cache_info().misses, 2)
                    self.assertEqual(module.translations.cache_info().hits, 1)

    def test_legacy_file_global_patch_keeps_with_name_lookup(self):
        module = self.modules[0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "descriptions_zh_tw.json").write_text(
                '{"translations":{"123":{"text":"探索世界"}}}', encoding="utf-8"
            )
            with patch.object(module, "__file__", str(root / "legacy.py")):
                self.assertEqual(module.translations(), {"123": {"text": "探索世界"}})

    def test_canonical_file_global_patch_resolves_repository_document(self):
        module = self.modules[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "descriptions_zh_tw.json").write_text(
                '{"translations":{"123":null}}', encoding="utf-8"
            )
            with patch.object(
                module,
                "__file__",
                str(root / "radar_backend" / "adapters" / "localized_descriptions.py"),
            ):
                self.assertEqual(module.translations(), {"123": None})

    def test_current_global_callbacks_are_resolved_at_the_original_call(self):
        for module in self.modules:
            with self.subTest(module=module.__name__):
                calls = []

                def clean(value):
                    calls.append(("clean", value))
                    module.is_chinese = (
                        lambda text: calls.append(("updated_chinese", text)) or True
                    )
                    return value or ""

                converter = SimpleNamespace(
                    convert=lambda value: calls.append(("convert", value))
                    or "converted"
                )
                with (
                    patch.object(module, "is_chinese"),
                    patch.object(module, "clean_description", side_effect=clean),
                    patch.object(module, "CONVERTER", converter),
                    patch.object(
                        module,
                        "translations",
                        side_effect=AssertionError("should stay lazy"),
                    ),
                ):
                    result = module.description_fields(
                        123, "English", "traditional", None
                    )
                self.assertEqual(result["short_description"], "converted")
                self.assertEqual(
                    calls[-2:],
                    [("updated_chinese", "traditional"), ("convert", "traditional")],
                )

    def test_converter_attribute_is_not_accessed_when_translation_unavailable(self):
        for module in self.modules:
            with (
                self.subTest(module=module.__name__),
                patch.object(module, "CONVERTER", None),
                patch.object(module, "translations", return_value={}),
            ):
                self.assertEqual(
                    module.description_fields(123, "English", None, None)[
                        "short_description_source"
                    ],
                    "unavailable",
                )

    def test_clean_callback_is_bound_once_before_the_three_value_map(self):
        for module in self.modules:
            with self.subTest(module=module.__name__):
                calls = []

                def clean(value):
                    calls.append(value)
                    module.clean_description = lambda _: "replaced helper"
                    return value or ""

                with patch.object(module, "clean_description", side_effect=clean):
                    result = module.description_fields(123, "English", "探索世界", None)
                self.assertEqual(calls, ["English", "探索世界", None])
                self.assertEqual(result["short_description"], "探索世界")

    def test_facade_hash_lookup_occurs_after_translation_document_get(self):
        for module in self.modules:
            with self.subTest(module=module.__name__):
                calls = []

                class Document:
                    def get(self, key, default):
                        calls.append(("translation_get", key))
                        return {}

                digest = SimpleNamespace(
                    hexdigest=lambda: calls.append(("hexdigest",)) or "digest"
                )
                hash_module = SimpleNamespace(
                    sha256=lambda value: calls.append(("sha256", value)) or digest
                )
                with (
                    patch.object(module, "translations", return_value=Document()),
                    patch.object(module, "hashlib", hash_module),
                ):
                    module.description_fields(123, "English", None, None)
                self.assertEqual(
                    calls,
                    [
                        ("translation_get", "123"),
                        ("sha256", b"English"),
                        ("hexdigest",),
                    ],
                )


def _description_case(
    english, traditional, simplified, document, expected_source, expected_text
):
    def test(self):
        for module in self.modules:
            with (
                self.subTest(module=module.__name__),
                patch.object(module, "translations", return_value=document),
            ):
                fields = module.description_fields(
                    123, english, traditional, simplified
                )
                self.assertEqual(fields["short_description_source"], expected_source)
                self.assertEqual(fields["short_description"], expected_text)
                self.assertEqual(
                    fields["short_description_language"],
                    "zh-TW" if expected_text else "",
                )
                self.assertEqual(
                    fields["short_description_en"], domain.clean_description(english)
                )

    return test


_FINGERPRINT = domain.description_fingerprint("English")
for _index, _case in enumerate(
    (
        ("English", "探索世界", None, {}, "steam_tchinese", "探索世界"),
        ("English", "探索世", None, {}, "unavailable", ""),
        ("English", "English", "探索世界", {}, "steam_schinese_converted", "探索世界"),
        ("English", "探索世界の", None, {}, "unavailable", ""),
        ("English", "探索世界・", None, {}, "steam_tchinese", "探索世界・"),
        (
            "English",
            None,
            None,
            {"123": {"source_sha256": _FINGERPRINT, "text": "探索世界"}},
            "editorial_zh_tw",
            "探索世界",
        ),
        (
            "Changed English",
            None,
            None,
            {"123": {"source_sha256": _FINGERPRINT, "text": "探索世界"}},
            "unavailable",
            "",
        ),
        (
            "English",
            None,
            None,
            {"123": {"source_sha256": _FINGERPRINT, "text": "探索世界の"}},
            "unavailable",
            "",
        ),
        (
            "English",
            None,
            None,
            {"123": {"source_sha256": _FINGERPRINT, "text": "探索世"}},
            "unavailable",
            "",
        ),
        (
            None,
            None,
            None,
            {
                "123": {
                    "source_sha256": domain.description_fingerprint(""),
                    "text": "探索世界",
                }
            },
            "unavailable",
            "",
        ),
        (
            "English",
            None,
            None,
            {123: {"source_sha256": _FINGERPRINT, "text": "探索世界"}},
            "unavailable",
            "",
        ),
        ("English", None, None, {"123": {"text": "探索世界"}}, "unavailable", ""),
        (
            "English",
            None,
            None,
            {"123": {"source_sha256": _FINGERPRINT}},
            "unavailable",
            "",
        ),
        (
            "English",
            None,
            None,
            {"123": {"source_sha256": _FINGERPRINT, "text": "探索世界"}, "124": {}},
            "editorial_zh_tw",
            "探索世界",
        ),
        (
            "English",
            "探索世界",
            "探索世界の",
            {"123": None},
            "steam_tchinese",
            "探索世界",
        ),
        (
            "English",
            "English",
            "探索世界",
            {"123": None},
            "steam_schinese_converted",
            "探索世界",
        ),
    )
):
    setattr(
        DescriptionFacadeTests,
        f"test_source_evidence_case_{_index:02d}",
        _description_case(*_case),
    )


if __name__ == "__main__":
    unittest.main()
