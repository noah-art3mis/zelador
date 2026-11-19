"""Differential test of `zel validate`: the Rust port must expand every changeset
exactly as the Python CLI does – same stdout, stderr, exit code, plan file bytes
and API requests. Harness in tests/rust_harness.py.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from tests.conftest import FakeZotero, make_collection, make_item
from tests.rust_harness import (
    INSTANTS,
    _scripted,
    normalise,
    request_log,
    run_python,
    run_rust,
    snapshot,
)

GOLDEN_DIR = Path(__file__).parent / "golden"


# -- zel validate ----------------------------------------------------------------

# The registry every validate case expands against unless it says otherwise.
REGISTRY = """\
families:
  status: {description: workflow state, coloured: true, exclusive: true}
  rating: {coloured: true, exclusive: true}
  topic: {description: subject vocabulary}
tags:
  - tag: status:to-read
    aliases: [to-read, unread]
    colour: "#E69F00"
  - tag: status:read
    aliases: [read]
    colour: "#009E73"
  - tag: rating:fav
    colour: "#cc79a7"
  - tag: topic:ai
    aliases: [AI, Artificial Intelligence, "l'été"]
  - tag: topic:ml
"""
REGISTRY_COLOURS = [
    {"name": "status:to-read", "color": "#E69F00"},
    {"name": "status:read", "color": "#009E73"},
    {"name": "rating:fav", "color": "#cc79a7"},
]

# The golden fixture's registry (tests/test_golden_plan.py), as a file.
GOLDEN_REGISTRY = """\
families:
  status: {coloured: true, exclusive: true}
  topic: {}
tags:
  - {tag: status:to-read, colour: "#E69F00"}
  - {tag: status:read, colour: "#009E73"}
  - {tag: topic:ai, aliases: [AI, Artificial Intelligence]}
"""


def validate_fake(tag_colors=REGISTRY_COLOURS) -> FakeZotero:
    """A library with every shape validate distinguishes: items with and without
    a field, creators or tags, every attachment state, a type the schema
    endpoints refuse, a stock exclusivity violation, and a collection tree."""
    items = [
        make_item(
            "JOUR0001",
            version=5,
            title="Attention",
            tags=[{"tag": "AI", "type": 1}, {"tag": "keep-me"}],
            collections=["COLL0001"],
            DOI="10.1/x",
            creators=[{"creatorType": "author", "firstName": "Ashish", "lastName": "Vaswani"}],
        ),
        make_item(
            "JOUR0002",
            version=9,
            title="Deep learning",
            tags=[{"tag": "status:to-read"}, {"tag": "l'été"}],
            creators=[],
        ),
        make_item(
            "BOOK0001",
            version=11,
            item_type="book",
            title="Knowledge",
            publisher="Routledge",
            creators=[{"creatorType": "editor", "name": "Ed Itor"}],
        ),
        make_item(
            "WEBP0001",
            version=12,
            item_type="webpage",
            title="Blog",
            websiteTitle="Site",
            creators=[{"creatorType": "contributor", "name": "Cont"}],
        ),
        make_item(
            "ATTC0001",
            version=13,
            item_type="attachment",
            title="scan.pdf",
            tags=[{"tag": "unread"}, {"tag": "topic:ai"}],
            collections=["COLL0002", "COLL0003"],
        ),
        make_item("ATTC0002", version=14, item_type="attachment", title="bare.pdf"),
        make_item(
            "ATTC0003", version=15, item_type="attachment", title="kid.pdf", parentItem="JOUR0001"
        ),
        make_item("NOTE0001", version=16, item_type="note", title=""),
        make_item(
            "DUPL0001",
            version=17,
            title="Two statuses",
            tags=[{"tag": "status:to-read"}, {"tag": "status:read"}],
        ),
        make_item("TRSH0001", version=18, title="Trashed", deleted=True),
    ]
    collections = [
        make_collection("COLL0001", "Projects", version=3),
        make_collection("COLL0002", "Inbox", version=4),
        make_collection("COLL0003", "Thesis", version=5, parent="COLL0001"),
        make_collection("COLL0004", "Chapter 1", version=6, parent="COLL0003"),
        make_collection("COLL0005", "Old", version=7),
    ]
    collections[-1]["data"]["deleted"] = True
    settings = {} if tag_colors is None else {"tagColors": {"value": tag_colors, "version": 280}}
    return FakeZotero(
        items=items,
        collections=collections,
        settings=settings,
        library_version=300,
        page_size=3,
    )


def golden_fake() -> FakeZotero:
    from tests.test_golden_plan import library

    return library()


def cs(*intents, slug="case", **extra) -> dict:
    return {"schema": "changeset.v1", "slug": slug, "intents": list(intents), **extra}


def creator(**fields) -> dict:
    return {"creatorType": "author", **fields}


VALIDATE_CASES: dict[str, dict] = {
    # -- every op, one at a time ------------------------------------------------
    "merge_tag": {
        "changeset": cs({"op": "merge_tag", "from": ["AI", "l'été"], "into": "topic:ai"})
    },
    "merge_tag_into_held_tag": {
        "changeset": cs({"op": "merge_tag", "from": ["unread"], "into": "status:to-read"})
    },
    "add_tag": {
        "changeset": cs({"op": "add_tag", "tag": "topic:ml", "keys": ["JOUR0001", "JOUR0002"]})
    },
    "add_tag_already_held": {
        "changeset": cs({"op": "add_tag", "tag": "status:to-read", "keys": ["JOUR0002"]})
    },
    "remove_tag": {
        "changeset": cs({"op": "remove_tag", "tag": "keep-me", "keys": ["JOUR0001", "JOUR0002"]})
    },
    "fill_field_empty_is_low_risk": {
        "changeset": cs({"op": "fill_field", "key": "JOUR0002", "field": "volume", "value": "12"})
    },
    "fill_field_overwrite_is_high_risk": {
        "changeset": cs({"op": "fill_field", "key": "JOUR0001", "field": "DOI", "value": "10.1/y"})
    },
    "fill_field_same_value_is_noop": {
        "changeset": cs({"op": "fill_field", "key": "JOUR0001", "field": "DOI", "value": "10.1/x"})
    },
    "clear_field": {"changeset": cs({"op": "clear_field", "key": "JOUR0001", "field": "DOI"})},
    "clear_field_already_empty": {
        "changeset": cs({"op": "clear_field", "key": "JOUR0002", "field": "volume"})
    },
    "set_creators_on_empty_is_low_risk": {
        "changeset": cs(
            {"op": "set_creators", "key": "JOUR0002", "creators": [creator(name="Goodfellow")]}
        )
    },
    "set_creators_replacing_is_high_risk": {
        "changeset": cs(
            {
                "op": "set_creators",
                "key": "JOUR0001",
                "creators": [
                    creator(firstName="A.", lastName="Vaswani"),
                    {"creatorType": "editor", "name": "Ed"},
                ],
            }
        )
    },
    "set_item_type_clears_foreign_fields": {
        "changeset": cs({"op": "set_item_type", "key": "BOOK0001", "itemType": "journalArticle"})
    },
    "set_item_type_same_type_is_noop": {
        "changeset": cs({"op": "set_item_type", "key": "BOOK0001", "itemType": "book"})
    },
    "set_item_type_webpage_to_book": {
        "changeset": cs({"op": "set_item_type", "key": "WEBP0001", "itemType": "book"})
    },
    "create_item_plain": {
        "changeset": cs(
            {
                "op": "create_item",
                "itemType": "book",
                "fields": {"title": "Wittgenstein: l'été", "date": "1983"},
                "creators": [creator(firstName="David", lastName="Bloor")],
                "tags": ["status:to-read", "topic:ai"],
                "collections": ["COLL0001"],
            }
        )
    },
    "create_item_minimal": {
        "changeset": cs({"op": "create_item", "itemType": "webpage", "fields": {"title": "T"}})
    },
    "create_item_adopts_attachment": {
        "changeset": cs(
            {
                "op": "create_item",
                "itemType": "journalArticle",
                "fields": {"title": "Scanned"},
                "tags": ["topic:ai", "topic:ml"],
                "collections": ["COLL0001", "COLL0002"],
                "attachment": "ATTC0001",
            }
        )
    },
    "create_item_adopts_bare_attachment": {
        "changeset": cs(
            {
                "op": "create_item",
                "itemType": "journalArticle",
                "fields": {"title": "Bare"},
                "attachment": "ATTC0002",
            }
        )
    },
    "add_to_collection": {
        "changeset": cs(
            {"op": "add_to_collection", "collection": "COLL0002", "keys": ["JOUR0001", "JOUR0002"]}
        )
    },
    "remove_from_collection": {
        "changeset": cs(
            {
                "op": "remove_from_collection",
                "collection": "COLL0001",
                "keys": ["JOUR0001", "JOUR0002"],
            }
        )
    },
    "create_collection": {
        "changeset": cs(
            {"op": "create_collection", "name": "Archive"},
            {"op": "create_collection", "name": "Sub", "parent": "COLL0001"},
            {"op": "create_collection", "name": "Root", "parent": None},
        )
    },
    "rename_collection": {
        "changeset": cs(
            {"op": "rename_collection", "collection": "COLL0002", "name": "Caixa d'entrada"},
            {"op": "rename_collection", "collection": "COLL0001", "name": "Projects"},
        )
    },
    "move_collection": {
        "changeset": cs(
            {"op": "move_collection", "collection": "COLL0003", "parent": None},
            {"op": "move_collection", "collection": "COLL0004", "parent": "COLL0002"},
            {"op": "move_collection", "collection": "COLL0002", "parent": None},
        )
    },
    "trash_collection": {
        "changeset": cs(
            {"op": "trash_collection", "collection": "COLL0004"},
            {"op": "trash_collection", "collection": "COLL0005"},
        )
    },
    "trash_item": {
        "changeset": cs(
            {"op": "trash_item", "key": "JOUR0002"}, {"op": "trash_item", "key": "JOUR0002"}
        )
    },
    "json_outcome": {
        "changeset": cs({"op": "add_tag", "tag": "topic:ml", "keys": ["JOUR0001"]}),
        "args": ["--json"],
    },
    # -- composition: later ops see earlier results --------------------------------
    "compose_ops_on_one_item": {
        "changeset": cs(
            {"op": "merge_tag", "from": ["AI"], "into": "topic:ai"},
            {"op": "add_tag", "tag": "rating:fav", "keys": ["JOUR0001"]},
            {"op": "fill_field", "key": "JOUR0001", "field": "volume", "value": "30"},
            {"op": "fill_field", "key": "JOUR0001", "field": "volume", "value": "31"},
            {"op": "remove_tag", "tag": "topic:ai", "keys": ["JOUR0001"]},
            {"op": "add_to_collection", "collection": "COLL0002", "keys": ["JOUR0001"]},
            {"op": "trash_item", "key": "JOUR0001"},
            slug="compose",
        )
    },
    "compose_new_collection_then_move_under_it": {
        "changeset": cs(
            {"op": "move_collection", "collection": "COLL0004", "parent": None},
            {"op": "move_collection", "collection": "COLL0001", "parent": "COLL0004"},
        )
    },
    "compose_on_objects_created_earlier": {
        "changeset": cs(
            {"op": "create_item", "itemType": "book", "fields": {"title": "New"}},
            {"op": "add_tag", "tag": "status:read", "keys": ["NEWC0001"]},
            {"op": "fill_field", "key": "NEWC0001", "field": "publisher", "value": "P"},
            {"op": "create_collection", "name": "Fresh"},
            {"op": "move_collection", "collection": "COLL0002", "parent": "NEWC0002"},
            {"op": "add_to_collection", "collection": "NEWC0002", "keys": ["NEWC0001"]},
        )
    },
    "compose_created_item_exclusivity": {
        "changeset": cs(
            {
                "op": "create_item",
                "itemType": "book",
                "fields": {"title": "New"},
                "tags": ["status:read"],
            },
            {"op": "add_tag", "tag": "status:to-read", "keys": ["NEWC0001"]},
        )
    },
    "retry_then_ok": {
        "changeset": cs({"op": "trash_item", "key": "JOUR0002"}),
        "fake": lambda: _scripted(
            validate_fake(), [httpx.Response(429, headers={"Retry-After": "0"})]
        ),
    },
    "golden": {
        "changeset": json.loads((GOLDEN_DIR / "changeset.json").read_text()),
        "fake": golden_fake,
        "registry": GOLDEN_REGISTRY,
        "backup": "20260719T115900Z",
        "instant": datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC),
        "golden": True,
    },
    # -- settings drift ---------------------------------------------------------
    "drift_when_colours_differ": {
        "changeset": cs({"op": "trash_item", "key": "JOUR0002"}),
        "fake": lambda: validate_fake([{"name": "ler", "color": "#FF0000"}]),
    },
    "drift_when_order_differs": {
        "changeset": cs({"op": "trash_item", "key": "JOUR0002"}),
        "fake": lambda: validate_fake(REGISTRY_COLOURS[::-1]),
        "args": ["--json"],
    },
    "no_drift_when_only_key_order_differs": {
        "changeset": cs({"op": "trash_item", "key": "JOUR0002"}),
        "fake": lambda: validate_fake(
            [{"color": c["color"], "name": c["name"]} for c in REGISTRY_COLOURS]
        ),
    },
    "drift_when_setting_unset": {
        "changeset": cs({"op": "trash_item", "key": "JOUR0002"}),
        "fake": lambda: validate_fake(None),
    },
    "no_registry_no_settings": {
        "changeset": cs({"op": "trash_item", "key": "JOUR0002"}),
        "fake": lambda: validate_fake([{"name": "ler", "color": "#FF0000"}]),
        "registry": None,
    },
    # -- semantic failures: every one collected, exit 2 ------------------------
    "fail_unknown_key": {
        "changeset": cs(
            {"op": "add_tag", "tag": "topic:ml", "keys": ["NOPE0001", "JOUR0001"]},
            {"op": "fill_field", "key": "NOPE0002", "field": "title", "value": "x"},
            {"op": "trash_item", "key": "TRSH0001"},
            {"op": "rename_collection", "collection": "NOPE0003", "name": "x"},
        )
    },
    "fail_alias_and_non_canonical": {
        "changeset": cs(
            {"op": "add_tag", "tag": "l'été", "keys": ["JOUR0001"]},
            {"op": "add_tag", "tag": "Misc", "keys": ["JOUR0001"]},
            {"op": "merge_tag", "from": ["x"], "into": "read"},
        )
    },
    "fail_merge_canonical_away": {
        "changeset": cs(
            {"op": "merge_tag", "from": ["topic:ml", "AI", "topic:ai"], "into": "topic:ai"}
        )
    },
    "fail_tags_without_registry": {
        "changeset": cs(
            {"op": "add_tag", "tag": "topic:ml", "keys": ["JOUR0001"]},
            {"op": "merge_tag", "from": ["AI"], "into": "topic:ai"},
            {"op": "remove_tag", "tag": "AI", "keys": ["JOUR0001"]},
        ),
        "registry": None,
    },
    "fail_extra_field": {
        "changeset": cs(
            {"op": "fill_field", "key": "JOUR0001", "field": "extra", "value": "x"},
            {"op": "clear_field", "key": "JOUR0001", "field": "extra"},
            {"op": "create_item", "itemType": "book", "fields": {"extra": "x", "title": "t"}},
        )
    },
    "fail_invalid_field": {
        "changeset": cs(
            {"op": "fill_field", "key": "JOUR0001", "field": "publisher", "value": "x"},
            {"op": "create_item", "itemType": "book", "fields": {"volume": "1", "DOI": "d"}},
        )
    },
    "fail_invalid_creator_type": {
        "changeset": cs(
            {
                "op": "set_creators",
                "key": "JOUR0001",
                "creators": [
                    {"creatorType": "composer", "name": "a"},
                    {"creatorType": "x", "name": "b"},
                ],
            },
            {
                "op": "create_item",
                "itemType": "webpage",
                "fields": {"title": "t"},
                "creators": [
                    {"creatorType": "editor", "name": "a"},
                    {"creatorType": "x", "name": "b"},
                ],
            },
        )
    },
    "fail_invalid_item_type": {
        "changeset": cs(
            {"op": "create_item", "itemType": "spaceship", "fields": {"title": "t"}},
            {"op": "set_item_type", "key": "JOUR0001", "itemType": "spaceship"},
        )
    },
    "fail_set_item_type_creator_mismatch": {
        "changeset": cs({"op": "set_item_type", "key": "BOOK0001", "itemType": "webpage"})
    },
    "fail_exclusivity": {
        "changeset": cs(
            {"op": "add_tag", "tag": "status:read", "keys": ["JOUR0002", "JOUR0001"]},
            {"op": "add_tag", "tag": "rating:fav", "keys": ["JOUR0001"]},
            {
                "op": "create_item",
                "itemType": "book",
                "fields": {"title": "t"},
                "tags": ["status:read", "status:to-read", "rating:fav"],
            },
        )
    },
    "stock_exclusivity_violation_is_not_a_veto": {
        "changeset": cs(
            {"op": "add_tag", "tag": "topic:ml", "keys": ["DUPL0001"]},
            {"op": "fill_field", "key": "DUPL0001", "field": "volume", "value": "2"},
        )
    },
    "removing_a_stock_exclusivity_violation": {
        "changeset": cs({"op": "remove_tag", "tag": "status:read", "keys": ["DUPL0001"]})
    },
    "fail_exclusivity_on_merge": {
        "changeset": cs(
            {"op": "merge_tag", "from": ["keep-me"], "into": "status:read"},
            {"op": "add_tag", "tag": "status:to-read", "keys": ["JOUR0001"]},
        )
    },
    "fail_missing_collections": {
        "changeset": cs(
            {"op": "add_to_collection", "collection": "NOPE0001", "keys": ["JOUR0001"]},
            {
                "op": "create_item",
                "itemType": "book",
                "fields": {"title": "t"},
                "collections": ["NOPE0002", "COLL0001", "NOPE0003"],
            },
            {"op": "create_collection", "name": "x", "parent": "NOPE0004"},
            {"op": "move_collection", "collection": "COLL0001", "parent": "NOPE0005"},
            {"op": "trash_collection", "collection": "NOPE0006"},
        )
    },
    "fail_move_would_cycle": {
        "changeset": cs(
            {"op": "move_collection", "collection": "COLL0001", "parent": "COLL0004"},
            {"op": "move_collection", "collection": "COLL0003", "parent": "COLL0003"},
        )
    },
    "fail_attachment_not_adoptable": {
        "changeset": cs(
            {
                "op": "create_item",
                "itemType": "book",
                "fields": {"title": "a"},
                "attachment": "JOUR0001",
            },
            {
                "op": "create_item",
                "itemType": "book",
                "fields": {"title": "b"},
                "attachment": "ATTC0003",
            },
            {
                "op": "create_item",
                "itemType": "book",
                "fields": {"title": "c"},
                "attachment": "NOPE0001",
            },
        )
    },
    "fail_json_outcome": {
        "changeset": cs({"op": "add_tag", "tag": "AI", "keys": ["NOPE0001"]}),
        "args": ["--json"],
    },
    "fail_schema_lookup_for_unknown_stored_type": {
        "changeset": cs({"op": "fill_field", "key": "NOTE0001", "field": "title", "value": "x"})
    },
    # -- structural lint: exit 2 before any request ---------------------------------
    "lint_missing_file": {"changeset": None},
    "lint_not_json_empty": {"changeset": ""},
    "lint_not_json_trailing_comma": {"changeset": '{"schema": "changeset.v1",\n  "intents": [1,]}'},
    "lint_not_json_object_comma": {"changeset": '{"a": 1,}'},
    "lint_not_json_missing_colon": {"changeset": '{"a" 1}'},
    "lint_not_json_missing_comma": {"changeset": '{"a": [1 2]}'},
    "lint_not_json_property_name": {"changeset": "{\n\t'a': 1}"},
    "lint_not_json_extra_data": {"changeset": '{"a": 1}\r\n\r\n  x'},
    "lint_not_json_bad_escape": {"changeset": '{"é": "a\\qb"}'},
    "lint_not_json_bad_unicode_escape": {"changeset": '["\\u12zz"]'},
    "lint_not_json_short_unicode_escape": {"changeset": '"\\u12"'},
    "lint_not_json_bad_surrogate_pair": {"changeset": '["\\ud800\\uzzzz"]'},
    "lint_not_json_unterminated": {"changeset": '{"a": "abc'},
    "lint_not_json_control_char": {"changeset": '{"a": "a\tb"}'},
    "lint_not_json_unclosed": {"changeset": '{"a": ['},
    "lint_not_json_bom": {"changeset": "﻿{}"},
    "lint_not_json_bad_literal": {"changeset": "[nul]"},
    "lint_not_json_number": {"changeset": "[-]"},
    # The top object plus 999 arrays: 1000 levels, the port's limit, under a key
    # validate ignores.
    "deep_nesting_at_the_limit": {
        "changeset": '{"schema": "changeset.v1", "slug": "deep", "note": '
        + "[" * 999
        + "]" * 999
        + ', "intents": [{"op": "trash_item", "key": "JOUR0002"}]}'
    },
    "lint_not_an_object": {"changeset": "[1, 2]"},
    "lint_schema_missing": {"changeset": {"slug": "x", "intents": []}},
    "lint_schema_wrong": {"changeset": {"schema": {"v": 1.0, "ok": True, "n": None}}},
    "lint_slug_missing": {"changeset": {"schema": "changeset.v1"}},
    "lint_slug_not_kebab": {"changeset": {"schema": "changeset.v1", "slug": "Bad Slug"}},
    "lint_slug_number": {"changeset": {"schema": "changeset.v1", "slug": 12}},
    "lint_slug_leading_dash": {"changeset": {"schema": "changeset.v1", "slug": "-x"}},
    "lint_slug_trailing_newline_passes": {
        "changeset": cs({"op": "trash_item", "key": "JOUR0002"}, slug="nl\n")
    },
    "lint_slug_two_newlines": {"changeset": {"schema": "changeset.v1", "slug": "nl\n\n"}},
    "lint_intents_missing": {"changeset": {"schema": "changeset.v1", "slug": "x"}},
    "lint_intents_empty": {"changeset": cs()},
    "lint_intents_not_list": {"changeset": cs(slug="x") | {"intents": {"op": "trash_item"}}},
    "lint_intent_not_mapping": {"changeset": cs(["trash_item"])},
    "lint_unknown_op": {"changeset": cs({"op": "delete_item", "key": "JOUR0001"})},
    "lint_op_missing": {"changeset": cs({"key": "JOUR0001"})},
    "lint_unknown_fields": {
        "changeset": cs({"op": "trash_item", "key": "K", "why": 1, "because": 2})
    },
    "lint_missing_field": {"changeset": cs({"op": "merge_tag", "into": "topic:ai"})},
    "lint_str_empty": {"changeset": cs({"op": "trash_item", "key": ""})},
    "lint_str_number": {"changeset": cs({"op": "trash_item", "key": 1.5e-7})},
    "lint_str_null": {"changeset": cs({"op": "trash_item", "key": None})},
    "lint_str_bool": {"changeset": cs({"op": "trash_item", "key": False})},
    "lint_str_or_null_empty": {
        "changeset": cs({"op": "move_collection", "collection": "C", "parent": ""})
    },
    "lint_str_or_null_missing": {"changeset": cs({"op": "move_collection", "collection": "C"})},
    "lint_str_or_null_list": {
        "changeset": cs({"op": "create_collection", "name": "n", "parent": ["a"]})
    },
    "lint_str_list_empty": {"changeset": cs({"op": "add_tag", "tag": "t", "keys": []})},
    "lint_str_list_blank": {"changeset": cs({"op": "add_tag", "tag": "t", "keys": ["a", ""]})},
    "lint_str_list_numbers": {
        "changeset": cs({"op": "add_tag", "tag": "t", "keys": [1, 100000.0]})
    },
    "lint_str_list_string": {"changeset": cs({"op": "merge_tag", "from": "AI", "into": "t"})},
    "lint_optional_str_list": {
        "changeset": cs(
            {"op": "create_item", "itemType": "book", "fields": {"t": "x"}, "tags": [None]}
        )
    },
    "lint_str_map_empty": {
        "changeset": cs({"op": "create_item", "itemType": "book", "fields": {}})
    },
    "lint_str_map_blank_value": {
        "changeset": cs({"op": "create_item", "itemType": "book", "fields": {"title": ""}})
    },
    "lint_str_map_nested": {
        "changeset": cs(
            {"op": "create_item", "itemType": "book", "fields": {"title": {"a": [True]}}}
        )
    },
    "lint_str_map_list": {
        "changeset": cs({"op": "create_item", "itemType": "book", "fields": ["title"]})
    },
    "lint_optional_str": {
        "changeset": cs(
            {"op": "create_item", "itemType": "book", "fields": {"t": "x"}, "attachment": 0}
        )
    },
    "lint_creators_empty": {"changeset": cs({"op": "set_creators", "key": "K", "creators": []})},
    "lint_creators_not_list": {
        "changeset": cs({"op": "set_creators", "key": "K", "creators": {"name": "x"}})
    },
    "lint_creator_not_mapping": {
        "changeset": cs({"op": "set_creators", "key": "K", "creators": ["rui's"]})
    },
    "lint_creator_unknown_keys": {
        "changeset": cs(
            {"op": "set_creators", "key": "K", "creators": [creator(name="a", lastname="b", z=1)]}
        )
    },
    "lint_creator_no_type": {
        "changeset": cs({"op": "set_creators", "key": "K", "creators": [{"name": "a"}]})
    },
    "lint_creator_empty_type": {
        "changeset": cs(
            {"op": "set_creators", "key": "K", "creators": [{"creatorType": "", "name": "a"}]}
        )
    },
    "lint_creator_both_forms": {
        "changeset": cs(
            {"op": "set_creators", "key": "K", "creators": [creator(name="a", lastName="b")]}
        )
    },
    "lint_creator_neither_form": {
        "changeset": cs({"op": "set_creators", "key": "K", "creators": [creator()]})
    },
    "lint_creator_empty_name": {
        "changeset": cs(
            {
                "op": "create_item",
                "itemType": "book",
                "fields": {"t": "x"},
                "creators": [creator(firstName="", lastName="b")],
            }
        )
    },
    "lint_first_bad_intent_wins": {
        "changeset": cs({"op": "trash_item", "key": "K"}, {"op": "trash_item"}, {"op": "nope"})
    },
    "lint_path_normalised": {"changeset": None, "path": "changesets//./absent.json"},
    # -- the registry and the backup gate: exit 1 -----------------------------------
    "no_backup": {"changeset": cs({"op": "trash_item", "key": "JOUR0002"}), "backup": None},
}


TRIVIAL = cs({"op": "trash_item", "key": "JOUR0002"})
FAMILIES = "families:\n  status: {coloured: true, exclusive: true}\n  topic: {}\n"

VALIDATE_CASES["yaml11_exclusive_no"] = {
    "changeset": cs({"op": "add_tag", "tag": "status:read", "keys": ["JOUR0002"]}),
    "registry": "families:\n  status: {exclusive: no}\n"
    + "tags:\n  - {tag: status:read}\n  - {tag: status:to-read}\n",
}
VALIDATE_CASES["yaml11_bool_aliases"] = {
    "changeset": cs(
        {"op": "add_tag", "tag": "no", "keys": ["JOUR0002"]},
        {"op": "add_tag", "tag": "True", "keys": ["JOUR0002"]},
        {"op": "add_tag", "tag": "False", "keys": ["JOUR0002"]},
        {"op": "add_tag", "tag": "yes", "keys": ["JOUR0002"]},
    ),
    "registry": "families:\n  topic: {}\ntags:\n  - {tag: topic:ai, aliases: [yes, 'no', NO]}\n",
}

# Defective registries: validate stops before any request, exit 1, as audit does.
REGISTRY_CASES = {
    "not_a_mapping": "- a\n- b\n",
    "empty_file": "",
    "families_not_mapping": "families: [status]\ntags: []\n",
    "tags_missing": "families: {}\n",
    "tags_not_list": "families: {}\ntags: {a: 1}\n",
    "family_not_mapping": "families:\n  status: [1, 2.5, true, null]\ntags: []\n",
    "family_unknown_keys": "families:\n  status: {color: true, colored: x}\ntags: []\n",
    "family_numeric_name": "families:\n  12: oops\ntags: []\n",
    "tag_entry_not_mapping": FAMILIES + "tags:\n  - status:read\n",
    "tag_entry_unknown_keys": FAMILIES + "tags:\n  - {tag: status:read, color: '#E69F00'}\n",
    "tag_not_family_value": FAMILIES + "tags:\n  - tag: Status:Read\n",
    "tag_missing": FAMILIES + "tags:\n  - {description: d}\n",
    "tag_number": FAMILIES + "tags:\n  - {tag: 12}\n",
    "tag_trailing_newline_passes": FAMILIES + 'tags:\n  - {tag: "topic:x\\n"}\n',
    "undeclared_family": FAMILIES + "tags:\n  - tag: rating:fav\n",
    "duplicate_canonical": FAMILIES + "tags:\n  - tag: topic:ai\n  - tag: topic:ai\n",
    "alias_shadows_canonical": FAMILIES
    + "tags:\n  - tag: topic:ai\n  - {tag: topic:ml, aliases: [topic:ai]}\n",
    "alias_two_owners": FAMILIES
    + "tags:\n  - {tag: topic:ai, aliases: [done]}\n  - {tag: topic:ml, aliases: [done]}\n",
    "aliases_as_a_string": FAMILIES
    + "tags:\n  - {tag: topic:ai, aliases: ab}\n  - {tag: topic:ml, aliases: [b]}\n",
    "colour_not_hex": FAMILIES + "tags:\n  - {tag: status:read, colour: 'E69F00'}\n",
    "colour_too_long": FAMILIES + "tags:\n  - {tag: status:read, colour: '#E69F00A'}\n",
    "colour_illegible": FAMILIES + "tags:\n  - {tag: status:read, colour: '#f0e442'}\n",
    "colour_on_uncoloured_family": FAMILIES + "tags:\n  - {tag: topic:ai, colour: '#E69F00'}\n",
    "too_many_coloured": FAMILIES
    + "tags:\n"
    + "".join(f"  - {{tag: 'status:s{i}', colour: '#E69F00'}}\n" for i in range(10)),
    "falsy_colour_is_uncoloured": FAMILIES + "tags:\n  - {tag: topic:ai, colour: ''}\n",
    "family_flags_truthy": "families:\n  status: {coloured: 1, exclusive: yes please}\n"
    + "  topic:\ntags:\n  - {tag: status:read, colour: '#009E73', description: 3}\n",
    # PyYAML resolves YAML 1.1 plain scalars that serde_yaml_ng leaves as text.
    "yaml11_coloured_no": "families:\n  status: {coloured: no}\n"
    + "tags:\n  - {tag: status:read, colour: '#009E73'}\n",
    "yaml11_on_off_and_quoted_no": "families:\n  status: {coloured: Off, exclusive: ON}\n"
    + "  topic: {coloured: 'no'}\ntags:\n  - {tag: topic:ai, colour: '#009E73'}\n",
    "yaml11_bool_family_name": "families:\n  on: {}\n  topic: {}\ntags:\n  - tag: on:x\n",
    "yaml11_single_letters_are_text": "families:\n  status: {coloured: y, exclusive: n}\n"
    + "tags:\n  - {tag: status:read, colour: '#009E73'}\n",
    "yaml11_octal_family_name": "families:\n  010: {}\ntags:\n  - tag: 010:x\n",
    "yaml11_underscore_zero_flag": "families:\n  status: {coloured: 0_0}\n"
    + "tags:\n  - {tag: status:read, colour: '#009E73'}\n",
    "yaml11_sexagesimal_tag": FAMILIES + "tags:\n  - tag: 1:30\n",
    "yaml11_numbers_repr": "families:\n  status: [.inf, -.Inf, .5, 1_0.5, 1:30.5, 0x_1F, 0b1_0,"
    + " 123456789012345678901234567890, ~, '']\ntags: []\n",
    "merge_keys": "base: &flags {coloured: true, exclusive: true}\n"
    + "extra: &desc {description: d, exclusive: false}\nfamilies:\n"
    + "  status: {<<: *flags, description: s}\n  rating: {<<: [*desc, *flags]}\n"
    + "  topic: {<<: *desc}\ntags:\n  - {tag: status:read, colour: '#009E73'}\n"
    + "  - {tag: rating:fav, colour: '#CC79A7'}\n",
    "merge_key_quoted_is_a_key": "families:\n  status: {'<<': {coloured: true}}\ntags: []\n",
}
for _name, _text in REGISTRY_CASES.items():
    VALIDATE_CASES[f"registry_{_name}"] = {"changeset": TRIVIAL, "registry": _text}
    VALIDATE_CASES[f"registry_{_name}_audit"] = {
        "changeset": TRIVIAL,
        "registry": _text,
        "command": ["audit", "tags"],
    }


def seed_backup(root: Path, timestamp: str | None) -> None:
    """The newest backup validate pins its plan to, or none at all."""
    root.mkdir(parents=True)
    if timestamp is not None:
        (root / "backups").mkdir()
        (root / "backups" / f"{timestamp}.jsonl").write_text(
            json.dumps({"kind": "header", "library_version": 1, "timestamp": timestamp}) + "\n"
        )


def write_changeset_file(root: Path, name: str, changeset) -> None:
    if changeset is None:
        return
    text = changeset if isinstance(changeset, str) else json.dumps(changeset, indent=2)
    (root / "changesets").mkdir()
    # Bytes, so a test's \r\n reaches the file as written.
    (root / "changesets" / f"{name}.json").write_bytes(text.encode())


@pytest.mark.parametrize("name", sorted(VALIDATE_CASES))
def test_rust_validate_matches_python(
    rust_bin, tmp_path, monkeypatch, isolated_config_dir, test_tz, name
):
    case = VALIDATE_CASES[name]
    registry = case.get("registry", REGISTRY)
    if registry is not None:
        isolated_config_dir.mkdir(parents=True)
        (isolated_config_dir / "taxonomy.yaml").write_text(registry)
    instant = case.get("instant", INSTANTS["default"])
    results, files = [], []
    for side in ("py", "rs"):
        data = tmp_path / side / "data"
        seed_backup(data, case.get("backup", "20261101T120000Z"))
        write_changeset_file(data, name, case["changeset"])
        # Joined as text, not with pathlib, so an unnormalised path reaches the CLI.
        target = f"{data}/{case.get('path', f'changesets/{name}.json')}"
        args = case.get("command") or ["validate", target, *case.get("args", [])]
        fake = case.get("fake", validate_fake)()
        if side == "py":
            outcome = run_python(args, fake, data, monkeypatch, instant)
        else:
            outcome = run_rust(rust_bin, args, fake, data, instant)
        results.append((normalise(outcome, data), request_log(fake)))
        files.append(snapshot(data))
    (expected, expected_requests), (actual, actual_requests) = results
    assert actual == expected
    assert files[1] == files[0]
    assert actual_requests == expected_requests
    if case.get("golden"):
        assert (
            files[1]["plans/20260719T120000Z-golden-sweep.json"]
            == (GOLDEN_DIR / "plan.json").read_text()
        )


TOO_DEEP = {
    # One level past the limit: refused with CPython-style position text.
    "one_too_deep": (
        '{"x": ' + "[" * 1000 + "]" * 1000 + "}",
        "nesting deeper than 1000 levels: line 1 column 1006 (char 1005)",
    ),
    # Deep enough to overflow a naive recursive parser's stack.
    "stack_buster": (
        "[" * 300_000,
        "nesting deeper than 1000 levels: line 1 column 1001 (char 1000)",
    ),
}


@pytest.mark.parametrize("name", sorted(TOO_DEEP))
def test_rust_refuses_deeper_nesting_cleanly(rust_bin, tmp_path, name):
    """Past 1000 levels the port refuses the changeset as bad input. CPython goes
    deeper, until its stack runs out (a RecursionError traceback), so this is a
    Rust-only check: a clean exit 2, never a crash."""
    text, reason = TOO_DEEP[name]
    data = tmp_path / "data"
    seed_backup(data, "20261101T120000Z")
    write_changeset_file(data, name, text)
    path = data / "changesets" / f"{name}.json"
    env = {"PATH": os.environ["PATH"], "ZELADOR_DATA_DIR": str(data), "HOME": str(tmp_path)}
    proc = subprocess.run(
        [str(rust_bin), "validate", str(path)], env=env, capture_output=True, text=True, timeout=60
    )
    assert (proc.returncode, proc.stdout) == (2, "")
    assert proc.stderr == f"error: {path} is not valid JSON: {reason}\n"
