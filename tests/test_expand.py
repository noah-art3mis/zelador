"""Tests for validate/expansion — intents against the live library into version-pinned plans."""

from datetime import UTC, datetime

import pytest

from tests.conftest import USER_ID, FakeZotero, make_collection, make_item
from zelador.client import ZoteroClient
from zelador.config import Credentials
from zelador.taxonomy import Family, TagEntry, Taxonomy
from zelador.write.contracts import Changeset
from zelador.write.expand import ValidationError, expand

NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)
BACKUP_TS = "20260719T115900Z"

TAX = Taxonomy(
    families={
        "status": Family(coloured=True, exclusive=True),
        "topic": Family(),
    },
    tags=(
        TagEntry(tag="status:to-read", colour="#E69F00"),
        TagEntry(tag="status:read", colour="#009E73"),
        TagEntry(tag="topic:ai", aliases=("AI", "Artificial Intelligence")),
    ),
)


def keygen():
    counter = iter(range(1, 100))
    return lambda: f"NEWC{next(counter):04d}"


def run_expand(
    intents,
    items=None,
    collections=None,
    settings=None,
    taxonomy=TAX,
    library_version=100,
):
    fake = FakeZotero(
        items=items or [],
        collections=collections or [],
        settings=settings or {},
        library_version=library_version,
        page_size=100,
    )
    client = ZoteroClient(
        Credentials(api_key="k", user_id=USER_ID), transport=fake.transport, sleep=lambda s: None
    )
    changeset = Changeset(slug="test-change", intents=intents)
    return expand(changeset, client, taxonomy, backup=BACKUP_TS, now=NOW, keygen=keygen())


def failures_of(intents, **kwargs) -> str:
    with pytest.raises(ValidationError) as exc_info:
        run_expand(intents, **kwargs)
    return "\n".join(exc_info.value.failures)


class TestPlanHeader:
    def test_header_binds_backup_and_library_version(self):
        plan = run_expand(
            [{"op": "add_tag", "tag": "topic:ai", "keys": ["AAAA1111"]}],
            items=[make_item("AAAA1111")],
            library_version=123,
        )
        assert plan.id == "20260719T120000Z-test-change"
        assert plan.backup == BACKUP_TS
        assert plan.library_version == 123
        assert plan.intents[0]["op"] == "add_tag"

    def test_settings_read_does_not_clobber_library_version(self):
        # The tagColors GET is a single-object request whose header carries the
        # setting's own (older) version — the plan must pin the library version.
        plan = run_expand(
            [{"op": "add_tag", "tag": "topic:ai", "keys": ["AAAA1111"]}],
            items=[make_item("AAAA1111")],
            settings={"tagColors": {"value": [], "version": 40}},
            library_version=123,
        )
        assert plan.library_version == 123


class TestMergeTag:
    def test_rewrites_each_carrying_item(self):
        items = [
            make_item(
                "AAAA1111",
                version=7,
                tags=[{"tag": "AI", "type": 1}, {"tag": "keep-me", "type": 0}],
            ),
            make_item("BBBB2222", version=9, tags=[{"tag": "Artificial Intelligence", "type": 1}]),
            make_item("CCCC3333", tags=[{"tag": "unrelated"}]),
        ]
        plan = run_expand(
            [{"op": "merge_tag", "from": ["AI", "Artificial Intelligence"], "into": "topic:ai"}],
            items=items,
        )
        assert len(plan.operations) == 2
        first = plan.operations[0]
        assert first.key == "AAAA1111" and first.version == 7 and first.facet == "tags"
        assert first.old == [{"tag": "AI", "type": 1}, {"tag": "keep-me", "type": 0}]
        assert first.new == [{"tag": "keep-me", "type": 0}, {"tag": "topic:ai", "type": 0}]
        assert first.risk == "high"

    def test_item_already_canonical_just_drops_aliases(self):
        items = [
            make_item("AAAA1111", tags=[{"tag": "AI", "type": 1}, {"tag": "topic:ai", "type": 0}])
        ]
        plan = run_expand([{"op": "merge_tag", "from": ["AI"], "into": "topic:ai"}], items=items)
        assert plan.operations[0].new == [{"tag": "topic:ai", "type": 0}]

    def test_into_must_be_canonical(self):
        assert "canonical" in failures_of(
            [{"op": "merge_tag", "from": ["ai stuff"], "into": "AI"}],
            items=[make_item("AAAA1111", tags=[{"tag": "ai stuff"}])],
        )

    def test_merging_a_canonical_tag_away_is_refused(self):
        assert "canonical" in failures_of(
            [{"op": "merge_tag", "from": ["status:read"], "into": "topic:ai"}],
            items=[make_item("AAAA1111", tags=[{"tag": "status:read"}])],
        )

    def test_requires_taxonomy(self):
        assert "taxonomy" in failures_of(
            [{"op": "merge_tag", "from": ["AI"], "into": "topic:ai"}],
            items=[make_item("AAAA1111", tags=[{"tag": "AI"}])],
            taxonomy=None,
        )


class TestAddRemoveTag:
    def test_add_tag_low_risk_manual_type(self):
        plan = run_expand(
            [{"op": "add_tag", "tag": "topic:ai", "keys": ["AAAA1111"]}],
            items=[make_item("AAAA1111", version=4, tags=[{"tag": "x"}])],
        )
        op = plan.operations[0]
        assert op.new == [{"tag": "x"}, {"tag": "topic:ai", "type": 0}]
        assert op.risk == "low" and op.version == 4

    def test_add_tag_already_present_is_a_no_op(self):
        plan = run_expand(
            [{"op": "add_tag", "tag": "topic:ai", "keys": ["AAAA1111"]}],
            items=[make_item("AAAA1111", tags=[{"tag": "topic:ai", "type": 0}])],
        )
        assert plan.operations == []

    def test_add_tag_refuses_alias_output(self):
        message = failures_of(
            [{"op": "add_tag", "tag": "AI", "keys": ["AAAA1111"]}],
            items=[make_item("AAAA1111")],
        )
        assert "topic:ai" in message  # points at the canonical tag

    def test_add_tag_unknown_item(self):
        assert "ZZZZ9999" in failures_of(
            [{"op": "add_tag", "tag": "topic:ai", "keys": ["ZZZZ9999"]}]
        )

    def test_remove_tag_high_risk_and_absent_is_no_op(self):
        plan = run_expand(
            [
                {"op": "remove_tag", "tag": "junk", "keys": ["AAAA1111", "BBBB2222"]},
            ],
            items=[
                make_item("AAAA1111", tags=[{"tag": "junk"}, {"tag": "keep"}]),
                make_item("BBBB2222", tags=[{"tag": "keep"}]),
            ],
        )
        assert len(plan.operations) == 1
        assert plan.operations[0].risk == "high"
        assert plan.operations[0].new == [{"tag": "keep"}]


class TestExclusivity:
    def test_introducing_second_status_tag_is_refused(self):
        message = failures_of(
            [{"op": "add_tag", "tag": "status:read", "keys": ["AAAA1111"]}],
            items=[make_item("AAAA1111", tags=[{"tag": "status:to-read", "type": 0}])],
        )
        assert "status" in message and "AAAA1111" in message

    def test_preexisting_violation_untouched_is_tolerated(self):
        items = [
            make_item(
                "AAAA1111",
                tags=[{"tag": "status:to-read", "type": 0}, {"tag": "status:read", "type": 0}],
            )
        ]
        plan = run_expand(
            [{"op": "add_tag", "tag": "topic:ai", "keys": ["AAAA1111"]}], items=items
        )
        assert len(plan.operations) == 1


class TestFillField:
    def test_filling_empty_field_is_low_risk(self):
        plan = run_expand(
            [{"op": "fill_field", "key": "AAAA1111", "field": "volume", "value": "12"}],
            items=[make_item("AAAA1111", version=6)],
        )
        op = plan.operations[0]
        assert op.facet == "field:volume" and op.old == "" and op.new == "12"
        assert op.risk == "low"

    def test_overwriting_is_high_risk(self):
        plan = run_expand(
            [{"op": "fill_field", "key": "AAAA1111", "field": "volume", "value": "12"}],
            items=[make_item("AAAA1111", volume="11")],
        )
        assert plan.operations[0].risk == "high"
        assert plan.operations[0].old == "11"

    def test_extra_is_never_a_target(self):
        assert "extra" in failures_of(
            [{"op": "fill_field", "key": "AAAA1111", "field": "extra", "value": "x"}],
            items=[make_item("AAAA1111")],
        )

    def test_field_checked_against_item_type_schema(self):
        assert "publisher" in failures_of(
            [{"op": "fill_field", "key": "AAAA1111", "field": "publisher", "value": "MIT"}],
            items=[make_item("AAAA1111", item_type="journalArticle")],
        )

    def test_same_value_is_a_no_op(self):
        plan = run_expand(
            [{"op": "fill_field", "key": "AAAA1111", "field": "volume", "value": "12"}],
            items=[make_item("AAAA1111", volume="12")],
        )
        assert plan.operations == []


class TestClearField:
    def test_clearing_writes_empty_and_is_high_risk(self):
        plan = run_expand(
            [{"op": "clear_field", "key": "AAAA1111", "field": "DOI"}],
            items=[make_item("AAAA1111", version=6, DOI="10.1/wrong")],
        )
        op = plan.operations[0]
        assert op.facet == "field:DOI" and op.old == "10.1/wrong" and op.new == ""
        assert op.risk == "high"

    def test_clearing_an_already_empty_field_is_a_no_op(self):
        plan = run_expand(
            [{"op": "clear_field", "key": "AAAA1111", "field": "DOI"}],
            items=[make_item("AAAA1111")],
        )
        assert plan.operations == []

    def test_extra_is_never_a_target(self):
        assert "extra" in failures_of(
            [{"op": "clear_field", "key": "AAAA1111", "field": "extra"}],
            items=[make_item("AAAA1111", extra="citekey")],
        )

    def test_field_checked_against_item_type_schema(self):
        assert "publisher" in failures_of(
            [{"op": "clear_field", "key": "AAAA1111", "field": "publisher"}],
            items=[make_item("AAAA1111", item_type="journalArticle")],
        )


class TestSetCreators:
    CREATORS = [{"creatorType": "author", "firstName": "Marcella", "lastName": "Castro"}]

    def test_setting_creators_on_bare_item_is_low_risk(self):
        plan = run_expand(
            [{"op": "set_creators", "key": "AAAA1111", "creators": self.CREATORS}],
            items=[make_item("AAAA1111", version=6)],
        )
        op = plan.operations[0]
        assert op.facet == "creators" and op.old == [] and op.new == self.CREATORS
        assert op.risk == "low"

    def test_replacing_creators_is_high_risk(self):
        old = [{"creatorType": "author", "lastName": "Wrong"}]
        plan = run_expand(
            [{"op": "set_creators", "key": "AAAA1111", "creators": self.CREATORS}],
            items=[make_item("AAAA1111", creators=old)],
        )
        assert plan.operations[0].risk == "high"
        assert plan.operations[0].old == old

    def test_same_creators_is_a_no_op(self):
        plan = run_expand(
            [{"op": "set_creators", "key": "AAAA1111", "creators": self.CREATORS}],
            items=[make_item("AAAA1111", creators=list(self.CREATORS))],
        )
        assert plan.operations == []

    def test_creator_type_checked_against_item_type_schema(self):
        creators = [{"creatorType": "recipient", "lastName": "X"}]
        assert "recipient" in failures_of(
            [{"op": "set_creators", "key": "AAAA1111", "creators": creators}],
            items=[make_item("AAAA1111", item_type="journalArticle")],
        )


class TestSetItemType:
    def test_type_change_emits_high_risk_item_type_facet(self):
        plan = run_expand(
            [{"op": "set_item_type", "key": "AAAA1111", "itemType": "book"}],
            items=[make_item("AAAA1111", version=6)],
        )
        op = plan.operations[0]
        assert op.facet == "itemType" and op.old == "journalArticle" and op.new == "book"
        assert op.risk == "high"

    def test_fields_invalid_in_new_type_are_cleared_alongside(self):
        plan = run_expand(
            [{"op": "set_item_type", "key": "AAAA1111", "itemType": "book"}],
            items=[make_item("AAAA1111", volume="3", DOI="10.1/x", url="https://x")],
        )
        cleared = {op.facet: op.old for op in plan.operations if op.facet != "itemType"}
        # volume and DOI are journalArticle-only; url is valid for book and survives
        assert cleared == {"field:volume": "3", "field:DOI": "10.1/x"}
        assert all(op.new == "" and op.risk == "high" for op in plan.operations
                   if op.facet != "itemType")

    def test_same_type_is_a_no_op(self):
        plan = run_expand(
            [{"op": "set_item_type", "key": "AAAA1111", "itemType": "journalArticle"}],
            items=[make_item("AAAA1111")],
        )
        assert plan.operations == []

    def test_unknown_type_fails(self):
        assert "notAType" in failures_of(
            [{"op": "set_item_type", "key": "AAAA1111", "itemType": "notAType"}],
            items=[make_item("AAAA1111")],
        )

    def test_creators_invalid_in_new_type_block_the_change(self):
        creators = [{"creatorType": "editor", "lastName": "X"}]
        assert "editor" in failures_of(
            [{"op": "set_item_type", "key": "AAAA1111", "itemType": "webpage"}],
            items=[make_item("AAAA1111", creators=creators)],
        )


class TestCreateItem:
    INTENT = {
        "op": "create_item",
        "itemType": "book",
        "fields": {"title": "Reason in Human Affairs", "date": "1983"},
        "creators": [{"creatorType": "author", "lastName": "Simon"}],
        "tags": ["status:to-read"],
    }

    def test_create_emits_object_op_with_generated_key(self):
        plan = run_expand([{**self.INTENT, "collections": ["COLL1111"]}],
                          collections=[make_collection("COLL1111", "Projects")])
        op = plan.operations[0]
        assert (op.op, op.kind, op.key, op.version, op.facet) == (
            "create_item", "item", "NEWC0001", 0, "object")
        assert op.old is None and op.risk == "low"
        assert op.new["itemType"] == "book" and op.new["title"] == "Reason in Human Affairs"
        assert op.new["tags"] == [{"tag": "status:to-read", "type": 0}]
        assert op.new["collections"] == ["COLL1111"]
        assert op.new["creators"][0]["lastName"] == "Simon"

    def test_fields_checked_against_type(self):
        bad = {**self.INTENT, "fields": {"publisher": "SUP", "volume": "3"}}
        assert "volume" in failures_of([bad])

    def test_extra_is_never_a_target(self):
        bad = {**self.INTENT, "fields": {"title": "T", "extra": "citekey"}}
        assert "extra" in failures_of([bad])

    def test_tags_must_be_canonical(self):
        assert "topic:ai" in failures_of([{**self.INTENT, "tags": ["AI"]}])

    def test_exclusive_family_enforced_within_the_new_item(self):
        bad = {**self.INTENT, "tags": ["status:to-read", "status:read"]}
        assert "exclusive" in failures_of([bad])

    def test_collections_must_exist(self):
        assert "COLL1111" in failures_of([{**self.INTENT, "collections": ["COLL1111"]}])

    def test_creators_checked_against_type(self):
        bad = {**self.INTENT, "creators": [{"creatorType": "recipient", "lastName": "X"}]}
        assert "recipient" in failures_of([bad])

    def test_unknown_item_type_fails(self):
        assert "notAType" in failures_of([{**self.INTENT, "itemType": "notAType"}])


class TestCreateItemAdoption:
    def intent(self, attachment="ATTA1111"):
        return {
            "op": "create_item",
            "itemType": "book",
            "fields": {"title": "Far-right publics on Brazilian Telegram"},
            "attachment": attachment,
        }

    def standalone(self, key="ATTA1111", **data):
        return make_item(key, version=5, item_type="attachment", title="384901eng-3", **data)

    def test_adoption_writes_parent_item_on_the_attachment(self):
        plan = run_expand([self.intent()], items=[self.standalone()])
        create, adopt = plan.operations
        assert create.key == "NEWC0001" and create.facet == "object"
        assert (adopt.op, adopt.key, adopt.version, adopt.facet) == (
            "create_item", "ATTA1111", 5, "parentItem")
        assert adopt.old is False and adopt.new == "NEWC0001" and adopt.risk == "low"

    def test_missing_attachment_fails(self):
        assert "ATTA1111" in failures_of([self.intent()])

    def facets(self, plan, key):
        return {op.facet: op for op in plan.operations if op.key == key}

    def test_the_attachments_tags_move_to_the_new_parent(self):
        """Zotero surfaces tags on items, so a tag left on a child is invisible —
        the user's hand-set rating:fav simply disappears from where they look."""
        att = self.standalone(tags=[{"tag": "rating:fav"}, {"tag": "status:read"}])
        plan = run_expand([self.intent()], items=[att])
        created = self.facets(plan, "NEWC0001")["object"].new
        assert [t["tag"] for t in created["tags"]] == ["rating:fav", "status:read"]
        cleared = self.facets(plan, "ATTA1111")["tags"]
        assert [t["tag"] for t in cleared.old] == ["rating:fav", "status:read"]
        assert cleared.new == []

    def test_declared_and_adopted_tags_union_without_duplicates(self):
        att = self.standalone(tags=[{"tag": "status:read"}, {"tag": "rating:fav"}])
        intent = {**self.intent(), "tags": ["topic:ai", "status:read"]}
        plan = run_expand([intent], items=[att])
        created = self.facets(plan, "NEWC0001")["object"].new
        assert [t["tag"] for t in created["tags"]] == ["topic:ai", "status:read", "rating:fav"]

    def test_an_unregistered_tag_on_the_attachment_does_not_block_adoption(self):
        """The registry governs what the agent may propose, not what the user
        already assigned — else one legacy tag makes a PDF unadoptable forever."""
        att = self.standalone(tags=[{"tag": "lido"}])
        plan = run_expand([self.intent()], items=[att])
        created = self.facets(plan, "NEWC0001")["object"].new
        assert [t["tag"] for t in created["tags"]] == ["lido"]

    def test_the_attachments_collections_move_too(self):
        """Zotero forbids child items in collections, so adoption drops the
        membership silently unless the parent picks it up."""
        att = self.standalone(collections=["COLL0001"])
        plan = run_expand([self.intent()], items=[att],
                          collections=[make_collection("COLL0001", "Somewhere")])
        created = self.facets(plan, "NEWC0001")["object"].new
        assert created["collections"] == ["COLL0001"]
        cleared = self.facets(plan, "ATTA1111")["collections"]
        assert cleared.old == ["COLL0001"] and cleared.new == []

    def test_an_attachment_with_neither_only_gets_a_parent(self):
        plan = run_expand([self.intent()], items=[self.standalone()])
        assert set(self.facets(plan, "ATTA1111")) == {"parentItem"}

    def test_non_attachment_item_refused(self):
        assert "not an attachment" in failures_of(
            [self.intent("AAAA1111")], items=[make_item("AAAA1111")])

    def test_already_parented_attachment_refused(self):
        items = [self.standalone(parentItem="BBBB2222")]
        assert "already" in failures_of([self.intent()], items=items)

    def test_trashed_attachment_is_invisible_like_all_trash(self):
        # expansion reads the live library without trash — a trashed attachment
        # is unknown, same as for every other op
        assert "no such item" in failures_of(
            [self.intent()], items=[self.standalone(deleted=True)])


class TestCollectionMembership:
    def test_add_to_collection(self):
        plan = run_expand(
            [{"op": "add_to_collection", "collection": "COLL1111", "keys": ["AAAA1111"]}],
            items=[make_item("AAAA1111", version=3, collections=["OTHER222"])],
            collections=[make_collection("COLL1111", "Shelf")],
        )
        op = plan.operations[0]
        assert op.facet == "collections" and op.kind == "item"
        assert op.old == ["OTHER222"] and op.new == ["OTHER222", "COLL1111"]
        assert op.risk == "low"

    def test_remove_from_collection_high_risk(self):
        plan = run_expand(
            [{"op": "remove_from_collection", "collection": "COLL1111", "keys": ["AAAA1111"]}],
            items=[make_item("AAAA1111", collections=["COLL1111"])],
            collections=[make_collection("COLL1111", "Shelf")],
        )
        assert plan.operations[0].new == [] and plan.operations[0].risk == "high"

    def test_unknown_collection_refused(self):
        assert "COLL9999" in failures_of(
            [{"op": "add_to_collection", "collection": "COLL9999", "keys": ["AAAA1111"]}],
            items=[make_item("AAAA1111")],
        )


class TestCollectionObjects:
    def test_create_collection_precomputes_key(self):
        plan = run_expand(
            [{"op": "create_collection", "name": "projects", "parent": "COLL1111"}],
            collections=[make_collection("COLL1111", "Root")],
        )
        op = plan.operations[0]
        assert op.kind == "collection" and op.facet == "object" and op.version == 0
        assert op.key == "NEWC0001"
        assert op.new == {"name": "projects", "parentCollection": "COLL1111"}
        assert op.risk == "low"

    def test_create_collection_parent_must_exist(self):
        assert "GONE9999" in failures_of(
            [{"op": "create_collection", "name": "projects", "parent": "GONE9999"}]
        )

    def test_rename_collection(self):
        plan = run_expand(
            [{"op": "rename_collection", "collection": "COLL1111", "name": "Better"}],
            collections=[make_collection("COLL1111", "Worse", version=5)],
        )
        op = plan.operations[0]
        assert op.facet == "name" and op.old == "Worse" and op.new == "Better"
        assert op.version == 5 and op.risk == "low"

    def test_move_collection_high_risk(self):
        plan = run_expand(
            [{"op": "move_collection", "collection": "CHLD1111", "parent": None}],
            collections=[
                make_collection("ROOT1111", "Root"),
                make_collection("CHLD1111", "Child", parent="ROOT1111"),
            ],
        )
        op = plan.operations[0]
        assert op.facet == "parentCollection" and op.old == "ROOT1111" and op.new is False
        assert op.risk == "high"

    def test_move_under_own_descendant_refused(self):
        message = failures_of(
            [{"op": "move_collection", "collection": "ROOT1111", "parent": "CHLD1111"}],
            collections=[
                make_collection("ROOT1111", "Root"),
                make_collection("CHLD1111", "Child", parent="ROOT1111"),
            ],
        )
        assert "descendant" in message

    def test_trash_collection_and_item(self):
        plan = run_expand(
            [
                {"op": "trash_collection", "collection": "COLL1111"},
                {"op": "trash_item", "key": "AAAA1111"},
            ],
            items=[make_item("AAAA1111")],
            collections=[make_collection("COLL1111", "Shelf")],
        )
        assert [(op.kind, op.facet, op.old, op.new, op.risk) for op in plan.operations] == [
            ("collection", "deleted", False, True, "high"),
            ("item", "deleted", False, True, "high"),
        ]


class TestCoalescing:
    def test_sequential_tag_ops_compose(self):
        items = [make_item("AAAA1111", version=7, tags=[{"tag": "AI", "type": 1}])]
        plan = run_expand(
            [
                {"op": "merge_tag", "from": ["AI"], "into": "topic:ai"},
                {"op": "add_tag", "tag": "status:read", "keys": ["AAAA1111"]},
            ],
            items=items,
        )
        merge, add = plan.operations
        assert merge.new == [{"tag": "topic:ai", "type": 0}]
        assert add.old == merge.new  # the second op sees the first's result
        assert add.new == [{"tag": "topic:ai", "type": 0}, {"tag": "status:read", "type": 0}]
        assert merge.version == add.version == 7  # one write, one pin

    def test_groups_index_into_intents(self):
        items = [make_item("AAAA1111", tags=[{"tag": "AI"}]), make_item("BBBB2222")]
        plan = run_expand(
            [
                {"op": "merge_tag", "from": ["AI"], "into": "topic:ai"},
                {"op": "add_tag", "tag": "status:read", "keys": ["BBBB2222"]},
            ],
            items=items,
        )
        assert [op.group for op in plan.operations] == [0, 1]
        assert [op.id for op in plan.operations] == ["op-001", "op-002"]


class TestSettingsDrift:
    def test_drift_adds_settings_entry(self):
        live = {"value": [{"name": "status:read", "color": "#009E73"}], "version": 90}
        plan = run_expand(
            [{"op": "add_tag", "tag": "topic:ai", "keys": ["AAAA1111"]}],
            items=[make_item("AAAA1111")],
            settings={"tagColors": live},
            library_version=100,
        )
        assert plan.settings == {
            "name": "tagColors",
            "version": 100,
            "old": [{"name": "status:read", "color": "#009E73"}],
            "new": [
                {"name": "status:to-read", "color": "#E69F00"},
                {"name": "status:read", "color": "#009E73"},
            ],
        }

    def test_no_drift_no_entry(self):
        live = {
            "value": [
                {"name": "status:to-read", "color": "#E69F00"},
                {"name": "status:read", "color": "#009E73"},
            ],
            "version": 90,
        }
        plan = run_expand(
            [{"op": "add_tag", "tag": "topic:ai", "keys": ["AAAA1111"]}],
            items=[make_item("AAAA1111")],
            settings={"tagColors": live},
        )
        assert plan.settings is None

    def test_no_taxonomy_no_entry(self):
        plan = run_expand(
            [{"op": "remove_tag", "tag": "junk", "keys": ["AAAA1111"]}],
            items=[make_item("AAAA1111", tags=[{"tag": "junk"}])],
            taxonomy=None,
        )
        assert plan.settings is None


class TestFailureAggregation:
    def test_all_failures_reported_at_once(self):
        with pytest.raises(ValidationError) as exc_info:
            run_expand(
                [
                    {"op": "add_tag", "tag": "topic:ai", "keys": ["ZZZZ9999"]},
                    {"op": "fill_field", "key": "AAAA1111", "field": "extra", "value": "x"},
                ],
                items=[make_item("AAAA1111")],
            )
        assert len(exc_info.value.failures) == 2
