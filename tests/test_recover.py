"""Tests for the recovery utilities — zel debug reconcile and zel debug restore."""

import json
from datetime import UTC, datetime

import pytest

from tests.conftest import USER_ID, FakeZotero, make_collection, make_item
from zelador.client import ZoteroClient
from zelador.config import Credentials
from zelador.status import pending_sessions
from zelador.write.changelog import SessionLog, read_log
from zelador.write.recover import RestoreError, run_reconcile, run_restore

NOW = datetime(2026, 7, 19, 13, 30, 0, tzinfo=UTC)
SESSION = "20260719T120000Z-crashed"


def client_for(fake: FakeZotero) -> ZoteroClient:
    return ZoteroClient(
        Credentials(api_key="k", user_id=USER_ID), transport=fake.transport, sleep=lambda s: None
    )


def op_dict(op_id, key, facet, old, new, kind="item", version=1):
    return {
        "id": op_id, "group": 0, "op": "x", "kind": kind, "key": key,
        "version": version, "facet": facet, "old": old, "new": new, "risk": "low",
    }


def crashed_session(log_dir, operations):
    log = SessionLog(log_dir / f"{SESSION}.jsonl")
    log.start(plan=SESSION, backup="20260719T115900Z", timestamp="20260719T120100Z")
    log.pending(operations)
    return log.path


class TestReconcile:
    def test_landed_write_marked_applied(self, tmp_path):
        # The write went out, the response was lost: current state equals new.
        fake = FakeZotero(
            items=[make_item("AAAA1111", version=8, tags=[{"tag": "t:x", "type": 0}])]
        )
        crashed_session(
            tmp_path,
            [op_dict("op-001", "AAAA1111", "tags", [], [{"tag": "t:x", "type": 0}], version=1)],
        )
        counts = run_reconcile(SESSION, client_for(fake), tmp_path)
        assert counts == {"applied": 1, "failed": 0}
        _, entries = read_log(tmp_path / f"{SESSION}.jsonl")
        assert entries["op-001"].status == "applied"
        assert entries["op-001"].version == 8  # the object's current version
        assert pending_sessions(tmp_path) == []

    def test_reordered_landed_write_marked_applied(self, tmp_path):
        # The write landed; Zotero returns the membership set in its own order.
        # Comparing the facet as an ordered list calls a successful write lost,
        # so reconcile would re-report it as failed to the user.
        fake = FakeZotero(
            items=[make_item("AAAA1111", version=8, collections=["ACOLL222", "ZCOLL111"])]
        )
        crashed_session(
            tmp_path,
            [op_dict("op-001", "AAAA1111", "collections",
                     ["ZCOLL111"], ["ZCOLL111", "ACOLL222"], version=1)],
        )
        counts = run_reconcile(SESSION, client_for(fake), tmp_path)
        assert counts == {"applied": 1, "failed": 0}
        _, entries = read_log(tmp_path / f"{SESSION}.jsonl")
        assert entries["op-001"].status == "applied"

    def test_lost_write_marked_failed(self, tmp_path):
        # The crash hit before the request: current state still equals old.
        fake = FakeZotero(items=[make_item("AAAA1111", version=1, tags=[])])
        crashed_session(
            tmp_path,
            [op_dict("op-001", "AAAA1111", "tags", [], [{"tag": "t:x", "type": 0}], version=1)],
        )
        counts = run_reconcile(SESSION, client_for(fake), tmp_path)
        assert counts == {"applied": 0, "failed": 1}
        _, entries = read_log(tmp_path / f"{SESSION}.jsonl")
        assert entries["op-001"].status == "failed"

    def test_never_created_collection_marked_failed(self, tmp_path):
        fake = FakeZotero()
        crashed_session(
            tmp_path,
            [op_dict("op-001", "NEWC0001", "object", None,
                     {"name": "archive", "parentCollection": False},
                     kind="collection", version=0)],
        )
        counts = run_reconcile(SESSION, client_for(fake), tmp_path)
        assert counts == {"applied": 0, "failed": 1}

    def test_missing_session_refused(self, tmp_path):
        with pytest.raises(RestoreError, match="no session"):
            run_reconcile("nope", client_for(FakeZotero()), tmp_path)


class TestRestore:
    def backup_file(self, tmp_path):
        lines = [
            {"kind": "header", "library_version": 100, "timestamp": "20260719T115900Z"},
            {"kind": "item", "object": make_item(
                "AAAA1111", version=90, title="The original title", volume="1"
            )},
            {"kind": "collection", "object": make_collection("COLL1111", "Original shelf",
                                                             version=80)},
        ]
        path = tmp_path / "20260719T115900Z.jsonl"
        path.write_text("".join(json.dumps(line) + "\n" for line in lines))
        return path

    def test_pushes_backup_state_at_current_version(self, tmp_path):
        backup = self.backup_file(tmp_path)
        # The accident: title mangled, version moved on to 120.
        fake = FakeZotero(
            items=[make_item("AAAA1111", version=120, title="MANGLED", volume="99")],
            library_version=120,
        )
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        outcome = run_restore(backup, ["AAAA1111"], client_for(fake), log_dir, now=NOW)
        assert outcome.applied == 1 and outcome.failed == 0
        assert fake.items[0]["data"]["title"] == "The original title"
        assert fake.items[0]["data"]["volume"] == "1"

    def test_restore_is_logged_like_any_session(self, tmp_path):
        backup = self.backup_file(tmp_path)
        fake = FakeZotero(items=[make_item("AAAA1111", version=120, title="MANGLED")],
                          library_version=120)
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        run_restore(backup, ["AAAA1111"], client_for(fake), log_dir, now=NOW)
        log_path = log_dir / "20260719T133000Z-restore.jsonl"
        assert log_path.exists()
        _, entries = read_log(log_path)
        (entry,) = entries.values()
        assert entry.status == "applied"
        assert entry.operation["old"]["title"] == "MANGLED"  # what the accident left

    def test_restores_collections_too(self, tmp_path):
        backup = self.backup_file(tmp_path)
        fake = FakeZotero(
            collections=[make_collection("COLL1111", "Renamed by accident", version=95)],
            library_version=95,
        )
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        outcome = run_restore(backup, ["COLL1111"], client_for(fake), log_dir, now=NOW)
        assert outcome.applied == 1
        assert fake.collections[0]["data"]["name"] == "Original shelf"

    def test_dry_run_writes_nothing_and_keeps_no_log(self, tmp_path):
        backup = self.backup_file(tmp_path)
        fake = FakeZotero(items=[make_item("AAAA1111", version=120, title="MANGLED")],
                          library_version=120)
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        outcome = run_restore(backup, ["AAAA1111"], client_for(fake), log_dir, now=NOW,
                              dry_run=True)
        assert outcome.applied == 1  # would restore
        assert fake.items[0]["data"]["title"] == "MANGLED"
        assert [r for r in fake.requests if r.method == "POST"] == []
        assert list(log_dir.glob("*.jsonl")) == []

    def test_key_absent_from_backup_refused(self, tmp_path):
        backup = self.backup_file(tmp_path)
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        with pytest.raises(RestoreError, match="ZZZZ9999"):
            run_restore(backup, ["ZZZZ9999"], client_for(FakeZotero()), log_dir, now=NOW)
