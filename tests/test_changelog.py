"""Tests for the log.v1 write-ahead change log — append-only, last-status-wins fold."""

import json

import pytest

from zelador.write.changelog import (
    LogFormatError,
    SessionLog,
    read_log,
    unresolved_ops,
)

OP1 = {"id": "op-001", "kind": "item", "key": "AAAA1111", "facet": "tags", "old": [], "new": []}
OP2 = {"id": "op-002", "kind": "item", "key": "BBBB2222", "facet": "deleted", "old": False,
       "new": True}


def start_log(tmp_path):
    log = SessionLog(tmp_path / "20260719T120000Z-slug.jsonl")
    log.start(plan="20260719T120000Z-slug", backup="20260719T115900Z",
              timestamp="20260719T120100Z")
    return log


class TestWriteAhead:
    def test_pending_entries_precede_resolution(self, tmp_path):
        log = start_log(tmp_path)
        log.pending([OP1, OP2])
        lines = [json.loads(line) for line in log.path.read_text().splitlines()]
        assert lines[0]["kind"] == "header" and lines[0]["schema"] == "log.v1"
        assert [line["status"] for line in lines[1:]] == ["pending", "pending"]
        assert lines[1]["operation"]["old"] == []  # the old state rides the pending entry

    def test_resolution_is_appended_not_rewritten(self, tmp_path):
        log = start_log(tmp_path)
        log.pending([OP1])
        log.resolve("op-001", "applied", version=8389)
        lines = log.path.read_text().splitlines()
        assert len(lines) == 3  # header + pending + resolution: a crash never loses the record
        assert json.loads(lines[2]) == {
            "kind": "entry", "op": "op-001", "status": "applied", "version": 8389,
        }


class TestFold:
    def test_last_status_wins(self, tmp_path):
        log = start_log(tmp_path)
        log.pending([OP1, OP2])
        log.resolve("op-001", "applied", version=8389)
        header, entries = read_log(log.path)
        assert header["plan"] == "20260719T120000Z-slug"
        assert entries["op-001"].status == "applied"
        assert entries["op-001"].version == 8389
        assert entries["op-001"].operation == OP1
        assert entries["op-002"].status == "pending"

    def test_undone_supersedes_applied(self, tmp_path):
        log = start_log(tmp_path)
        log.pending([OP1])
        log.resolve("op-001", "applied", version=8389)
        log.resolve("op-001", "undone", version=8395)
        _, entries = read_log(log.path)
        assert entries["op-001"].status == "undone"

    def test_unresolved_ops(self, tmp_path):
        log = start_log(tmp_path)
        log.pending([OP1, OP2])
        log.resolve("op-002", "failed")
        assert unresolved_ops(log.path) == ["op-001"]


class TestForeignFiles:
    """A file in log/ that is not a log.v1 session log.

    The one-off reclamation script wrote its audit trail into the same directory
    with its own shape; `line["op"]` on its first `target` line raised KeyError
    deep inside the fold, which bricked `zel status` for every later session.
    """

    def reclaim_log(self, tmp_path):
        path = tmp_path / "reclaim-20260806T212726Z.jsonl"
        path.write_text(
            json.dumps({"kind": "header", "timestamp": "20260806T212726Z", "objects": 47})
            + "\n"
            + json.dumps({"kind": "target", "key": "AAAA1111", "itemType": "attachment"})
            + "\n"
        )
        return path

    def test_read_log_refuses_a_foreign_header_by_name(self, tmp_path):
        path = self.reclaim_log(tmp_path)
        with pytest.raises(LogFormatError, match="reclaim-20260806T212726Z"):
            read_log(path)

    def test_a_file_with_no_header_at_all_is_refused(self, tmp_path):
        path = tmp_path / "headerless.jsonl"
        path.write_text(json.dumps({"kind": "entry", "op": "op-001", "status": "pending"}) + "\n")
        with pytest.raises(LogFormatError):
            read_log(path)

    def test_a_real_log_still_reads(self, tmp_path):
        log = start_log(tmp_path)
        log.pending([OP1])
        assert unresolved_ops(log.path) == ["op-001"]
