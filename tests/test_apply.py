"""Tests for zel apply — preconditions, write-ahead ordering, composition, verification."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from tests.conftest import USER_ID, FakeZotero, make_item
from zelador.client import ZoteroClient, ZoteroError
from zelador.config import Credentials
from zelador.write.apply import ApplyRefused, check_preconditions, run_apply
from zelador.write.changelog import read_log
from zelador.write.contracts import Operation, Plan

NOW = datetime(2026, 7, 19, 12, 30, 0, tzinfo=UTC)
BACKUP_TS = "20260719T115900Z"


def make_op(
    op_id="op-001",
    key="AAAA1111",
    facet="tags",
    old=None,
    new=None,
    kind="item",
    version=1,
    group=0,
    op="add_tag",
    risk="low",
):
    return Operation(
        id=op_id, group=group, op=op, kind=kind, key=key, version=version,
        facet=facet, old=old, new=new, risk=risk,
    )


def make_plan(operations, settings=None, plan_id="20260719T120000Z-test-change"):
    return Plan(
        id=plan_id,
        slug="test-change",
        backup=BACKUP_TS,
        library_version=100,
        intents=[{"op": "add_tag", "tag": "t:x", "keys": ["AAAA1111"]}],
        operations=operations,
        settings=settings,
    )


@pytest.fixture
def dirs(tmp_path):
    backups = tmp_path / "backups"
    log = tmp_path / "log"
    backups.mkdir()
    log.mkdir()
    (backups / f"{BACKUP_TS}.jsonl").write_text(
        json.dumps({"kind": "header", "library_version": 100, "timestamp": BACKUP_TS}) + "\n"
    )
    return backups, log


def client_for(fake: FakeZotero) -> ZoteroClient:
    return ZoteroClient(
        Credentials(api_key="k", user_id=USER_ID), transport=fake.transport, sleep=lambda s: None
    )


class TestPreconditions:
    def test_missing_backup_refused(self, dirs):
        backups, log = dirs
        (backups / f"{BACKUP_TS}.jsonl").unlink()
        with pytest.raises(ApplyRefused, match="backup"):
            check_preconditions(make_plan([make_op()]), backups, log, big=False)

    def test_pending_session_refused(self, dirs):
        backups, log = dirs
        (log / "20260601T000000Z-old.jsonl").write_text(
            '{"kind": "header", "schema": "log.v1", "plan": "20260601T000000Z-old"}\n'
            '{"kind": "entry", "op": "op-001", "status": "pending", "operation": {}}\n'
        )
        with pytest.raises(ApplyRefused, match="pending"):
            check_preconditions(make_plan([make_op()]), backups, log, big=False)

    def test_big_plan_needs_flag(self, dirs):
        backups, log = dirs
        ops = [
            make_op(op_id=f"op-{i:03d}", key=f"K{i:04d}AAA", facet="deleted", old=False, new=True)
            for i in range(201)
        ]
        with pytest.raises(ApplyRefused, match="--big"):
            check_preconditions(make_plan(ops), backups, log, big=False)
        check_preconditions(make_plan(ops), backups, log, big=True)  # does not raise

    def test_already_applied_plan_refused(self, dirs):
        backups, log = dirs
        (log / "20260719T120000Z-test-change.jsonl").write_text(
            '{"kind": "header", "schema": "log.v1", "plan": "20260719T120000Z-test-change"}\n'
        )
        with pytest.raises(ApplyRefused, match="already"):
            check_preconditions(make_plan([make_op()]), backups, log, big=False)


class TestExecution:
    def test_coalesced_ops_become_one_write(self, dirs):
        backups, log = dirs
        fake = FakeZotero(
            items=[make_item("AAAA1111", version=1, tags=[{"tag": "old"}])], library_version=100
        )
        ops = [
            make_op(op_id="op-001", facet="tags", old=[{"tag": "old"}], new=[]),
            make_op(op_id="op-002", facet="field:volume", old="", new="7", op="fill_field"),
        ]
        outcome = run_apply(make_plan(ops), client_for(fake), backups, log, now=NOW)
        posts = [r for r in fake.requests if r.method == "POST"]
        assert len(posts) == 1
        body = json.loads(posts[0].content)
        assert body == [{"key": "AAAA1111", "version": 1, "tags": [], "volume": "7"}]
        assert outcome.applied == 2 and outcome.failed == 0
        _, entries = read_log(log / "20260719T120000Z-test-change.jsonl")
        assert entries["op-001"].status == "applied"
        assert entries["op-001"].version == entries["op-002"].version == 101

    def test_collections_written_before_items(self, dirs):
        backups, log = dirs
        fake = FakeZotero(items=[make_item("AAAA1111", version=1)], library_version=100)
        ops = [
            make_op(op_id="op-001", key="AAAA1111", facet="collections", old=[], new=["NEWC0001"]),
            make_op(
                op_id="op-002", key="NEWC0001", kind="collection", facet="object", version=0,
                old=None, new={"name": "archive", "parentCollection": False},
                op="create_collection",
            ),
        ]
        outcome = run_apply(make_plan(ops), client_for(fake), backups, log, now=NOW)
        posts = [r for r in fake.requests if r.method == "POST"]
        assert "/collections" in str(posts[0].url) and "/items" in str(posts[1].url)
        assert outcome.applied == 2
        assert fake.collections[0]["data"]["name"] == "archive"

    def test_pending_entries_survive_a_crash(self, dirs):
        backups, log = dirs
        fake = FakeZotero(items=[make_item("AAAA1111", version=1)])
        fake.script.append(httpx.Response(500, text="boom"))
        ops = [make_op(facet="tags", old=[], new=[{"tag": "t:x", "type": 0}])]
        with pytest.raises(ZoteroError):
            run_apply(make_plan(ops), client_for(fake), backups, log, now=NOW)
        _, entries = read_log(log / "20260719T120000Z-test-change.jsonl")
        assert entries["op-001"].status == "pending"  # the undo record was written first

    def test_version_conflict_marks_failed(self, dirs):
        backups, log = dirs
        fake = FakeZotero(items=[make_item("AAAA1111", version=9)], library_version=100)
        ops = [make_op(version=1, facet="tags", old=[], new=[{"tag": "t:x", "type": 0}])]
        outcome = run_apply(make_plan(ops), client_for(fake), backups, log, now=NOW)
        assert outcome.failed == 1 and outcome.applied == 0
        assert outcome.failures[0]["key"] == "AAAA1111"
        assert outcome.failures[0]["code"] == 412
        _, entries = read_log(log / "20260719T120000Z-test-change.jsonl")
        assert entries["op-001"].status == "failed"

    def test_unchanged_write_recorded(self, dirs):
        backups, log = dirs
        fake = FakeZotero(
            items=[make_item("AAAA1111", version=1, tags=[{"tag": "x"}])], library_version=100
        )
        ops = [make_op(facet="tags", old=[{"tag": "x"}], new=[{"tag": "x"}])]
        outcome = run_apply(make_plan(ops), client_for(fake), backups, log, now=NOW)
        assert outcome.unchanged == 1
        _, entries = read_log(log / "20260719T120000Z-test-change.jsonl")
        assert entries["op-001"].status == "unchanged"

    def test_settings_entry_written_and_logged(self, dirs):
        backups, log = dirs
        fake = FakeZotero(items=[make_item("AAAA1111", version=1)], library_version=100)
        settings = {"name": "tagColors", "version": 100, "old": [],
                    "new": [{"name": "status:read", "color": "#009E73"}]}
        ops = [make_op(facet="tags", old=[], new=[{"tag": "t:x", "type": 0}])]
        outcome = run_apply(make_plan(ops, settings=settings), client_for(fake),
                            backups, log, now=NOW)
        assert fake.settings["tagColors"]["value"] == settings["new"]
        assert outcome.applied == 2
        _, entries = read_log(log / "20260719T120000Z-test-change.jsonl")
        assert entries["settings"].status == "applied"

    def test_settings_write_is_one_put_pinned_to_the_plan(self, dirs):
        # No preliminary GET: the plan's library-version pin alone guards the
        # write — a post-validation change bumps the setting past it (412).
        backups, log = dirs
        fake = FakeZotero(
            items=[make_item("AAAA1111", version=1)],
            settings={"tagColors": {"value": [], "version": 40}},
            library_version=100,
        )
        settings = {"name": "tagColors", "version": 100, "old": [],
                    "new": [{"name": "t", "color": "#009E73"}]}
        ops = [make_op(facet="tags", old=[], new=[{"tag": "t:x", "type": 0}])]
        run_apply(make_plan(ops, settings=settings), client_for(fake), backups, log, now=NOW)
        settings_requests = [r for r in fake.requests if "settings/tagColors" in str(r.url)]
        assert [r.method for r in settings_requests] == ["PUT"]
        assert settings_requests[0].headers["If-Unmodified-Since-Version"] == "100"

    def test_drifted_setting_marks_failed_and_writes_nothing(self, dirs):
        # tagColors changed between validate and apply — its version moved past
        # the plan's pin, so the server refuses the write (412), not clobbered.
        backups, log = dirs
        drifted = {"value": [{"name": "hand-set", "color": "#000000"}], "version": 105}
        fake = FakeZotero(
            items=[make_item("AAAA1111", version=1)],
            settings={"tagColors": drifted},
            library_version=100,
        )
        settings = {"name": "tagColors", "version": 100, "old": [], "new": []}
        ops = [make_op(facet="tags", old=[], new=[{"tag": "t:x", "type": 0}])]
        outcome = run_apply(make_plan(ops, settings=settings), client_for(fake),
                            backups, log, now=NOW)
        assert outcome.failed == 1
        assert fake.settings["tagColors"]["value"] == drifted["value"]
        _, entries = read_log(log / "20260719T120000Z-test-change.jsonl")
        assert entries["settings"].status == "failed"


class TestVerification:
    def test_clean_apply_verifies(self, dirs):
        backups, log = dirs
        fake = FakeZotero(items=[make_item("AAAA1111", version=1)], library_version=100)
        ops = [make_op(facet="tags", old=[], new=[{"tag": "t:x", "type": 0}])]
        outcome = run_apply(make_plan(ops), client_for(fake), backups, log, now=NOW)
        assert outcome.verified is True and outcome.mismatches == []

    def test_server_reordered_tags_still_verify(self, dirs):
        # Zotero stores tags sorted; expand appends. The live list comes back
        # in a different order than the composed write — that is not a mismatch.
        backups, log = dirs
        fake = FakeZotero(
            items=[make_item("AAAA1111", version=1, tags=[{"tag": "zebra", "type": 1}])],
            library_version=100,
        )
        ops = [make_op(facet="tags", old=[{"tag": "zebra", "type": 1}],
                       new=[{"tag": "zebra", "type": 1}, {"tag": "t:x", "type": 0}])]
        outcome = run_apply(make_plan(ops), client_for(fake), backups, log, now=NOW)
        assert outcome.verified is True and outcome.mismatches == []

    def test_server_reordered_collections_still_verify(self, dirs):
        # An item's collection membership is a set: Zotero returns it in its own
        # order while expand appends, so a reordered list is not a mismatch.
        backups, log = dirs
        fake = FakeZotero(
            items=[make_item("AAAA1111", version=1, collections=["ZCOLL111"])],
            library_version=100,
        )
        ops = [make_op(facet="collections", op="add_to_collection",
                       old=["ZCOLL111"], new=["ZCOLL111", "ACOLL222"])]
        outcome = run_apply(make_plan(ops), client_for(fake), backups, log, now=NOW)
        assert outcome.verified is True and outcome.mismatches == []

    def test_create_and_adopt_apply_and_verify(self, dirs):
        backups, log = dirs
        fake = FakeZotero(
            items=[make_item("ATTA1111", version=7, item_type="attachment")],
            library_version=100,
            page_size=100,
        )
        created = {
            "itemType": "book",
            "title": "Far-right publics on Brazilian Telegram",
            "creators": [{"creatorType": "author", "lastName": "Cesarino"}],
            "tags": [{"tag": "status:to-read", "type": 0}],
            "collections": [],
        }
        ops = [
            make_op(op_id="op-001", key="NEWI1111", facet="object", old=None,
                    new=created, version=0, op="create_item"),
            make_op(op_id="op-002", key="ATTA1111", facet="parentItem", old=False,
                    new="NEWI1111", version=7, op="create_item"),
        ]
        outcome = run_apply(make_plan(ops), client_for(fake), backups, log, now=NOW)
        assert outcome.failed == 0 and outcome.verified is True
        by_key = {i["key"]: i for i in fake.items}
        assert by_key["NEWI1111"]["data"]["itemType"] == "book"
        assert by_key["ATTA1111"]["data"]["parentItem"] == "NEWI1111"

    def test_item_type_change_with_cleared_fields_verifies(self, dirs):
        backups, log = dirs
        fake = FakeZotero(
            items=[make_item("AAAA1111", version=1, volume="3")],
            library_version=100,
            page_size=100,
        )
        ops = [
            make_op(op_id="op-001", facet="itemType", old="journalArticle", new="book",
                    op="set_item_type", risk="high"),
            make_op(op_id="op-002", facet="field:volume", old="3", new="",
                    op="set_item_type", risk="high"),
        ]
        outcome = run_apply(make_plan(ops), client_for(fake), backups, log, now=NOW)
        assert outcome.failed == 0 and outcome.verified is True
        assert fake.items[0]["data"]["itemType"] == "book"
        assert "volume" not in fake.items[0]["data"]

    def test_lying_success_is_caught(self, dirs):
        backups, log = dirs
        fake = FakeZotero(items=[make_item("AAAA1111", version=1)], library_version=100)
        fake.script.append(
            httpx.Response(
                200,
                json={"success": {"0": "AAAA1111"}, "unchanged": {}, "failed": {}},
                headers={"Last-Modified-Version": "101"},
            )
        )
        ops = [make_op(facet="tags", old=[], new=[{"tag": "t:x", "type": 0}])]
        outcome = run_apply(make_plan(ops), client_for(fake), backups, log, now=NOW)
        assert outcome.verified is False
        assert any("AAAA1111" in m for m in outcome.mismatches)
