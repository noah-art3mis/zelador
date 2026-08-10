"""CLI tests for zel backup and zel status."""

import json

import httpx
import pytest
from typer.testing import CliRunner

from tests.conftest import FakeZotero, make_collection, make_item
from zelador import cli
from zelador.client import ZoteroClient
from zelador.config import Credentials

runner = CliRunner()


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setenv("ZELADOR_DATA_DIR", str(tmp_path / "data"))
    fake = FakeZotero(
        items=[make_item("AAAA1111", tags=[{"tag": "ai"}]), make_item("BBBB2222")],
        collections=[make_collection("CCCC0001", "projects")],
        settings={"tagColors": {"value": [], "version": 3}},
        library_version=42,
        page_size=100,
    )
    creds = Credentials(api_key="sekrit", user_id="11868292")

    def fake_client():
        return ZoteroClient(creds, transport=fake.transport, sleep=lambda s: None)

    monkeypatch.setattr(cli, "make_client", fake_client)
    return fake


class TestBackupCommand:
    def test_writes_backup_and_reports_outcome(self, fake, tmp_path):
        result = runner.invoke(cli.app, ["backup"])
        assert result.exit_code == 0
        files = list((tmp_path / "data" / "backups").glob("*.jsonl"))
        assert len(files) == 1
        assert files[0].name in result.output

    def test_json_outcome_object(self, fake):
        result = runner.invoke(cli.app, ["backup", "--json"])
        assert result.exit_code == 0
        outcome = json.loads(result.stdout.strip().splitlines()[-1])
        assert outcome["library_version"] == 42
        assert outcome["items"] == 2
        assert outcome["noop"] is False

    def test_second_run_is_verified_noop(self, fake):
        runner.invoke(cli.app, ["backup"])
        result = runner.invoke(cli.app, ["backup", "--json"])
        assert result.exit_code == 0
        outcome = json.loads(result.stdout.strip().splitlines()[-1])
        assert outcome["noop"] is True


class TestStatusCommand:
    def test_first_run_shows_missing_pieces(self, fake):
        result = runner.invoke(cli.app, ["status"])
        assert result.exit_code == 0
        assert "42" in result.output  # live library version
        assert "none" in result.output  # no backup yet

    def test_after_backup_shows_counts_and_marker(self, fake):
        runner.invoke(cli.app, ["backup"])
        result = runner.invoke(cli.app, ["status"])
        assert result.exit_code == 0
        assert "2 items" in result.output
        assert "version 42" in result.output

    def test_api_down_still_prints_local_half(self, fake):
        runner.invoke(cli.app, ["backup"])
        fake.script.append(httpx.Response(500, text="down"))
        result = runner.invoke(cli.app, ["status"])
        assert result.exit_code == 0
        assert "2 items" in result.output
        assert "500" in result.output

    def test_json_object(self, fake):
        runner.invoke(cli.app, ["backup"])
        result = runner.invoke(cli.app, ["status", "--json"])
        status = json.loads(result.stdout.strip())
        assert status["api"]["library_version"] == 42
        assert status["backup"]["items"] == 2
        assert status["pending_sessions"] == []
        assert status["config"]["config_yaml"] in (True, False)

    def test_pending_session_logs_surface(self, fake, tmp_path):
        log_dir = tmp_path / "data" / "log"
        log_dir.mkdir(parents=True)
        (log_dir / "20260701T000000Z-merge-tags.jsonl").write_text(
            '{"kind": "header", "schema": "log.v1", "plan": "20260701T000000Z-merge-tags"}\n'
            '{"kind": "entry", "op": "op-001", "status": "pending", "operation": {}}\n'
            '{"kind": "entry", "op": "op-002", "status": "pending", "operation": {}}\n'
            '{"kind": "entry", "op": "op-001", "status": "applied", "version": 43}\n'
        )
        result = runner.invoke(cli.app, ["status"])
        assert "20260701T000000Z-merge-tags" in result.output

    def test_fully_resolved_log_is_not_pending(self, fake, tmp_path):
        log_dir = tmp_path / "data" / "log"
        log_dir.mkdir(parents=True)
        (log_dir / "20260701T000000Z-done.jsonl").write_text(
            '{"kind": "header", "schema": "log.v1", "plan": "20260701T000000Z-done"}\n'
            '{"kind": "entry", "op": "op-001", "status": "pending", "operation": {}}\n'
            '{"kind": "entry", "op": "op-001", "status": "applied", "version": 43}\n'
        )
        result = runner.invoke(cli.app, ["status"])
        assert "20260701T000000Z-done" not in result.output

    def test_a_foreign_file_in_log_does_not_crash_status(self, fake, tmp_path):
        """The one-off reclamation script wrote its audit trail into log/ with its
        own shape; status used to die on `line["op"]` before printing anything."""
        log_dir = tmp_path / "data" / "log"
        log_dir.mkdir(parents=True)
        (log_dir / "reclaim-20260806T212726Z.jsonl").write_text(
            '{"kind": "header", "timestamp": "20260806T212726Z", "objects": 47}\n'
            '{"kind": "target", "key": "AAAA1111", "itemType": "attachment"}\n'
        )
        (log_dir / "20260701T000000Z-merge-tags.jsonl").write_text(
            '{"kind": "header", "schema": "log.v1", "plan": "20260701T000000Z-merge-tags"}\n'
            '{"kind": "entry", "op": "op-001", "status": "pending", "operation": {}}\n'
        )
        result = runner.invoke(cli.app, ["status"])
        assert result.exit_code == 0, result.output
        assert "20260701T000000Z-merge-tags" in result.output
        assert "reclaim-20260806T212726Z" in result.output

    def test_foreign_files_are_listed_in_the_json_object(self, fake, tmp_path):
        log_dir = tmp_path / "data" / "log"
        log_dir.mkdir(parents=True)
        (log_dir / "reclaim-20260806T212726Z.jsonl").write_text(
            '{"kind": "header", "timestamp": "20260806T212726Z", "objects": 47}\n'
        )
        result = runner.invoke(cli.app, ["status", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        assert payload["foreign_logs"] == ["reclaim-20260806T212726Z"]
        assert payload["pending_sessions"] == []
