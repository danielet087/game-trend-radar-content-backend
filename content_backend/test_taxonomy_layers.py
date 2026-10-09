"""Contracts shared by the pure taxonomy rules and both public facades."""

from __future__ import annotations

import copy
import html
import inspect
import json
import re
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import unquote, urlsplit

import steam_taxonomy as legacy
from radar_backend.adapters import steam_taxonomy as canonical
from radar_backend.domain import steam_taxonomy as domain

NAMES = {19: "Action", 21: "Adventure", 122: "RPG"}
ROWS = [{"tagid": 19, "name": "動作", "browseable": True}]
RELATED_TAGS = (
    "tag_ids",
    "tag_labels_zh_tw",
    "tag_labels_language",
    "tags_source",
    "tag_labels_source",
    "tags_checked_at",
)
RELATED_GENRES = (
    "genre_labels_zh_tw",
    "genre_labels_language",
    "genres_source",
    "genres_checked_at",
)


def page(rows=ROWS, *, language="zh-tw", appid=123, genre_html=None):
    details = ""
    if genre_html is not None:
        details = (
            '<div id="genresAndManufacturer"><b>類型：</b>'
            + genre_html
            + '<br><div class="dev_row">publisher</div></div>'
        )
    return (
        f'<html lang="{language}">{details}'
        f"<script>InitAppTagModal( {appid}, {json.dumps(rows)}, [] );</script></html>"
    )


class TaxonomyContractMixin:
    module = None

    def parse(self, body, appid=123, names=None):
        return self.module.parse_store_taxonomy(
            body, appid, NAMES if names is None else names
        )

    def test_explicit_empty_tags_are_successful(self):
        self.assertEqual(
            self.parse(page([])),
            {
                "tags": [],
                "tag_ids": {},
                "tag_labels_zh_tw": {},
                "genres": None,
                "genre_labels_zh_tw": {},
            },
        )

    def test_nonempty_invalid_rows_are_unavailable(self):
        rows = [
            None,
            [],
            "bad",
            3,
            True,
            {},
            {"tagid": True, "name": "假"},
            {"tagid": 0, "name": "零"},
            {"tagid": -1, "name": "負"},
            {"tagid": 19.0, "name": "浮點"},
            {"tagid": "19", "name": "字串"},
            {"tagid": 19, "name": None},
            {"tagid": 19, "name": " "},
            {"tagid": 19, "name": "<b></b>"},
            {"tagid": 19, "name": "動作", "browseable": False},
        ]
        self.assertIsNone(self.parse(page(rows)))

    def test_row_validation_and_duplicate_ids_preserve_order(self):
        rows = [
            None,
            {"tagid": 21, "name": " 冒險 ", "browseable": 0},
            {"tagid": 19, "name": "動作"},
            {"tagid": 21, "name": "重複"},
            {"tagid": 122, "name": "角色扮演", "browseable": None},
        ]
        result = self.parse(page(rows))
        self.assertEqual(result["tags"], ["Adventure", "Action", "RPG"])
        self.assertEqual(
            result["tag_labels_zh_tw"],
            {"Adventure": "冒險", "Action": "動作", "RPG": "角色扮演"},
        )

    def test_twenty_tag_limit_counts_valid_unique_ids(self):
        rows = [{"tagid": False, "name": "壞"}]
        for ident in range(1, 25):
            rows.extend([{"tagid": ident, "name": f"標籤{ident}"}] * 2)
        result = self.parse(page(rows), names={})
        self.assertEqual(
            result["tags"], [f"steam-tag:{ident}" for ident in range(1, 21)]
        )
        self.assertEqual(list(result["tag_ids"].values()), list(range(1, 21)))

    def test_unknown_or_empty_identity_name_uses_stable_id(self):
        rows = [{"tagid": 999, "name": " 新標籤 "}, {"tagid": 19, "name": "動作"}]
        result = self.parse(page(rows), names={19: ""})
        self.assertEqual(result["tags"], ["steam-tag:999", "steam-tag:19"])
        self.assertEqual(result["tag_labels_zh_tw"]["steam-tag:999"], "新標籤")

    def test_shared_identity_names_keep_original_dictionary_overwrite(self):
        rows = [
            {"tagid": 19, "name": "甲"},
            {"tagid": 21, "name": "乙"},
            {"tagid": 19, "name": "丙"},
        ]
        result = self.parse(page(rows), names={19: "same", 21: "same"})
        self.assertEqual(result["tags"], ["same", "same", "same"])
        self.assertEqual(result["tag_ids"], {"same": 19})
        self.assertEqual(result["tag_labels_zh_tw"], {"same": "丙"})

    def test_appid_and_language_must_match(self):
        for body in (
            page(appid=124),
            page(language="en"),
            page(language="zh-cn"),
            page(language="zh-TW-extra"),
            '<html lang="zh-tw">none</html>',
        ):
            with self.subTest(body=body):
                self.assertIsNone(self.parse(body))
        self.assertIsNotNone(self.parse(page(language="ZH-TW")))

    def test_original_language_markup_rules_are_preserved(self):
        self.assertIsNone(self.parse(page().replace('lang="zh-tw"', 'lang = "zh-tw"')))
        self.assertIsNotNone(self.parse(page().replace('lang="zh-tw"', "lang='zh-tw'")))
        self.assertIsNotNone(
            self.parse(page().replace('lang="zh-tw"', 'data-lang="zh-tw"'))
        )

    def test_invalid_json_shape_and_first_tag_modal_are_unavailable(self):
        for rows in (None, False, 4, "tags", {"tagid": 19, "name": "動作"}):
            with self.subTest(rows=rows):
                self.assertIsNone(self.parse(page(rows)))
        body = page().replace(json.dumps(ROWS), "[not valid json]")
        self.assertIsNone(self.parse(body))
        first_wrong = page(appid=999).replace("</html>", page())
        self.assertIsNone(self.parse(first_wrong))

    def test_text_strips_markup_before_decoding_and_collapses_spaces(self):
        self.assertEqual(
            self.module.text(" <b>動作</b>\n&amp;\t 冒險&nbsp; "), "動作 & 冒險"
        )
        self.assertEqual(self.module.text("&lt;b&gt;文字&lt;/b&gt;"), "<b>文字</b>")
        self.assertEqual(self.module.text("<br><span></span>"), "")
        with self.assertRaises(TypeError):
            self.module.text(None)

    def test_genres_are_unavailable_without_expected_label_section(self):
        self.assertIsNone(self.parse(page())["genres"])
        self.assertIsNone(
            self.parse(page(genre_html="無類型").replace("類型：", "Genre:"))["genres"]
        )
        self.assertIsNone(
            self.parse(
                page(genre_html="無類型").replace("genresAndManufacturer", "other")
            )["genres"]
        )

    def test_explicit_genre_section_can_be_successfully_empty(self):
        self.assertEqual(self.parse(page(genre_html=""))["genres"], [])
        self.assertEqual(
            self.parse(page(genre_html='<a href="/genre/Action/">動作</a>'))["genres"],
            [],
        )

    def test_genre_identity_labels_and_order_follow_official_urls(self):
        anchors = (
            '<a href="https://store.steampowered.com/genre/Adventure/?a=1&amp;b=2"> 冒險 </a>'
            '<a href="https://store.steampowered.com/genre/%E8%A7%92%E8%89%B2/">角色</a>'
            '<a href="https://store.steampowered.com/genre/Adventure/">重複</a>'
            '<a href="http://store.steampowered.com:80/genre/RPG/">角色<b>扮演</b></a>'
        )
        result = self.parse(page(genre_html=anchors))
        self.assertEqual(result["genres"], ["Adventure", "角色", "RPG"])
        self.assertEqual(
            result["genre_labels_zh_tw"],
            {"Adventure": "冒險", "角色": "角色", "RPG": "角色扮演"},
        )

    def test_genre_hosts_paths_and_publisher_line_are_filtered(self):
        anchors = (
            '<a href="https://example.test/genre/Bad/">外站</a>'
            '<a href="https://store.steampowered.com.evil.test/genre/Bad/">假站</a>'
            '<a href="https://store.steampowered.com/app/123">遊戲</a>'
            '<a href="https://store.steampowered.com/genre/">空名稱</a>'
            '<a href="https://store.steampowered.com/genre/Empty/"><b></b></a>'
            '<a href="https://store.steampowered.com/genre/Action/">動作</a>'
            '<br><a href="https://store.steampowered.com/genre/Publisher/">發行商</a>'
        )
        result = self.parse(page(genre_html=anchors))
        self.assertEqual(result["genres"], ["Action"])
        self.assertEqual(result["genre_labels_zh_tw"], {"Action": "動作"})

    def test_parser_retains_uncaught_input_and_url_errors(self):
        with self.assertRaises(TypeError):
            self.parse(None)
        body = page(genre_html='<a href="http://[broken/genre/Action/">動作</a>')
        with self.assertRaises(ValueError):
            self.parse(body)
        with self.assertRaises(AttributeError):
            self.parse(page(), names=[])

    def test_parsing_leaves_identity_mapping_unchanged(self):
        names = dict(NAMES)
        before = copy.deepcopy(names)
        first = self.parse(page(), names=names)
        second = self.parse(page(), names=names)
        self.assertEqual(names, before)
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertIsNot(first["tags"], second["tags"])
        self.assertIsNot(first["tag_ids"], second["tag_ids"])

    def test_success_and_status_without_retry_use_incoming_references(self):
        existing = {"tags": ["old"], "genres": ["old"]}
        for status in (None, "success", "complete", "Retry", True):
            incoming = {
                "tags": ["new"],
                "genres": ["new"],
                "tags_fetch_status": status,
                "genres_fetch_status": status,
            }
            result = self.module.preserve_taxonomy(existing, incoming)
            self.assertEqual(result, incoming)
            self.assertIsNot(result, incoming)
            self.assertIs(result["tags"], incoming["tags"])
            self.assertIs(result["genres"], incoming["genres"])

    def test_retry_keeps_existing_objects_and_incoming_retry_status(self):
        existing = {"tags": ["old"], "genres": ["old"], "other": ["old"]}
        for key in RELATED_TAGS + RELATED_GENRES:
            existing[key] = {"old": key}
        incoming = {
            "tags": [],
            "genres": [],
            "other": ["new"],
            "tags_fetch_status": "retry",
            "genres_fetch_status": "retry",
        }
        before_existing, before_incoming = copy.deepcopy(existing), copy.deepcopy(
            incoming
        )
        result = self.module.preserve_taxonomy(existing, incoming)
        for key in ("tags", "genres") + RELATED_TAGS + RELATED_GENRES:
            self.assertIs(result[key], existing[key])
        self.assertIs(result["other"], incoming["other"])
        self.assertEqual(result["tags_fetch_status"], "retry")
        self.assertEqual(result["genres_fetch_status"], "retry")
        self.assertEqual(existing, before_existing)
        self.assertEqual(incoming, before_incoming)

    def test_retry_removes_related_fields_missing_from_existing(self):
        incoming = {
            "tags": [],
            "genres": [],
            "tags_fetch_status": "retry",
            "genres_fetch_status": "retry",
        }
        incoming.update({key: "new" for key in RELATED_TAGS + RELATED_GENRES})
        result = self.module.preserve_taxonomy({"tags": None, "genres": []}, incoming)
        self.assertIsNone(result["tags"])
        self.assertEqual(result["genres"], [])
        for key in RELATED_TAGS + RELATED_GENRES:
            self.assertNotIn(key, result)
            self.assertIn(key, incoming)

    def test_retry_without_existing_main_field_keeps_incoming(self):
        existing = {"tag_ids": {"orphan": 19}, "genre_labels_zh_tw": {"orphan": "舊"}}
        incoming = {
            "tags": [],
            "genres": [],
            "tag_ids": {},
            "genre_labels_zh_tw": {},
            "tags_fetch_status": "retry",
            "genres_fetch_status": "retry",
        }
        self.assertEqual(self.module.preserve_taxonomy(existing, incoming), incoming)

    def test_retry_is_independent_between_tags_and_genres(self):
        existing = {"tags": ["old-tag"], "genres": ["old-genre"]}
        incoming = {
            "tags": ["new-tag"],
            "genres": ["new-genre"],
            "tags_fetch_status": "retry",
            "genres_fetch_status": "success",
        }
        result = self.module.preserve_taxonomy(existing, incoming)
        self.assertIs(result["tags"], existing["tags"])
        self.assertIs(result["genres"], incoming["genres"])
        incoming["tags_fetch_status"], incoming["genres_fetch_status"] = (
            "success",
            "retry",
        )
        result = self.module.preserve_taxonomy(existing, incoming)
        self.assertIs(result["tags"], incoming["tags"])
        self.assertIs(result["genres"], existing["genres"])


class DomainTaxonomyTests(TaxonomyContractMixin, unittest.TestCase):
    module = domain


class CanonicalTaxonomyTests(TaxonomyContractMixin, unittest.TestCase):
    module = canonical


class LegacyTaxonomyTests(TaxonomyContractMixin, unittest.TestCase):
    module = legacy


class FacadePortTests(unittest.TestCase):
    def test_public_signatures_and_constants_are_preserved(self):
        signatures = {
            "text": "(value: 'str') -> 'str'",
            "parse_store_taxonomy": "(body: 'str', appid: 'int', tag_names: 'dict[int, str]') -> 'dict | None'",
            "preserve_taxonomy": "(existing: 'dict', incoming: 'dict') -> 'dict'",
        }
        for module in (legacy, canonical):
            for name, signature in signatures.items():
                self.assertEqual(
                    str(inspect.signature(getattr(module, name))), signature
                )
            self.assertEqual(
                module.TAG_LIST,
                "https://api.steampowered.com/IStoreService/GetTagList/v1/",
            )
            self.assertEqual(
                module.STORE_PAGE, "https://store.steampowered.com/app/{appid}/"
            )

    def test_current_text_callback_is_used_for_tags_and_genres(self):
        for module in (legacy, canonical):
            callback = Mock(side_effect=lambda value: "processed:" + value)
            with patch.object(module, "text", callback):
                result = module.parse_store_taxonomy(
                    page(
                        genre_html='<a href="https://store.steampowered.com/genre/Action/">類型</a>'
                    ),
                    123,
                    NAMES,
                )
            self.assertEqual(result["tag_labels_zh_tw"]["Action"], "processed:動作")
            self.assertEqual(result["genre_labels_zh_tw"]["Action"], "processed:類型")
            self.assertEqual(
                [call.args[0] for call in callback.call_args_list], ["動作", "類型"]
            )

    def test_current_html_and_regex_objects_are_used_by_text(self):
        for module in (legacy, canonical):
            escaped = Mock(wraps=html.unescape)
            regex = Mock(wraps=re)
            with patch.object(
                module, "html", SimpleNamespace(unescape=escaped)
            ), patch.object(module, "re", regex):
                self.assertEqual(module.text(" <b>動作</b> &amp; 冒險 "), "動作 & 冒險")
            escaped.assert_called_once_with(" 動作 &amp; 冒險 ")
            self.assertEqual(regex.sub.call_count, 2)

    def test_current_decoder_handles_original_body_tail(self):
        for module in (legacy, canonical):
            decoder = Mock()
            decoder.raw_decode.return_value = ([{"tagid": 21, "name": "patched"}], 1)
            factory = Mock(return_value=decoder)
            with patch.object(module, "json", SimpleNamespace(JSONDecoder=factory)):
                result = module.parse_store_taxonomy(page(), 123, NAMES)
            self.assertEqual(result["tags"], ["Adventure"])
            self.assertTrue(
                decoder.raw_decode.call_args.args[0].startswith(json.dumps(ROWS))
            )
            factory.assert_called_once_with()

    def test_current_decoder_value_and_type_errors_remain_unavailable(self):
        for module in (legacy, canonical):
            for error in (ValueError("decode"), TypeError("decode")):
                decoder = SimpleNamespace(raw_decode=Mock(side_effect=error))
                with patch.object(
                    module, "json", SimpleNamespace(JSONDecoder=lambda: decoder)
                ):
                    self.assertIsNone(module.parse_store_taxonomy(page(), 123, NAMES))
            decoder = SimpleNamespace(
                raw_decode=Mock(side_effect=RuntimeError("decode"))
            )
            with patch.object(
                module, "json", SimpleNamespace(JSONDecoder=lambda: decoder)
            ):
                with self.assertRaisesRegex(RuntimeError, "decode"):
                    module.parse_store_taxonomy(page(), 123, NAMES)

    def test_current_url_helpers_and_html_apply_to_genres(self):
        for module in (legacy, canonical):
            escaped = Mock(wraps=html.unescape)
            split = Mock(wraps=urlsplit)
            decode = Mock(side_effect=lambda value: "patched:" + unquote(value))
            body = page(
                genre_html='<a href="https://store.steampowered.com/genre/Action/?a=1&amp;b=2">動作</a>'
            )
            with patch.object(
                module, "html", SimpleNamespace(unescape=escaped)
            ), patch.object(module, "urlsplit", split), patch.object(
                module, "unquote", decode
            ):
                result = module.parse_store_taxonomy(body, 123, NAMES)
            self.assertEqual(result["genres"], ["patched:Action"])
            split.assert_called_once_with(
                "https://store.steampowered.com/genre/Action/?a=1&b=2"
            )
            decode.assert_called_once_with("Action")
            self.assertEqual(escaped.call_count, 3)

    def test_current_regex_object_is_used_throughout_parser(self):
        for module in (legacy, canonical):
            regex = Mock(wraps=re)
            regex.I, regex.S = re.I, re.S
            with patch.object(module, "re", regex):
                result = module.parse_store_taxonomy(page(genre_html=""), 123, NAMES)
            self.assertEqual(result["genres"], [])
            self.assertEqual(regex.search.call_count, 4)
            self.assertEqual(regex.sub.call_count, 2)
            self.assertEqual(regex.findall.call_count, 1)

    def test_facade_helper_rebinding_does_not_change_other_facade(self):
        with patch.object(legacy, "text", return_value="legacy-only"):
            self.assertEqual(
                legacy.parse_store_taxonomy(page(), 123, NAMES)["tag_labels_zh_tw"],
                {"Action": "legacy-only"},
            )
            self.assertEqual(
                canonical.parse_store_taxonomy(page(), 123, NAMES)["tag_labels_zh_tw"],
                {"Action": "動作"},
            )
        with patch.object(canonical, "text", return_value="canonical-only"):
            self.assertEqual(
                canonical.parse_store_taxonomy(page(), 123, NAMES)["tag_labels_zh_tw"],
                {"Action": "canonical-only"},
            )
            self.assertEqual(
                legacy.parse_store_taxonomy(page(), 123, NAMES)["tag_labels_zh_tw"],
                {"Action": "動作"},
            )


if __name__ == "__main__":
    unittest.main()
