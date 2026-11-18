"""CLI tests for the change loop — zel validate/apply/undo and debug reconcile/restore."""

import json

import pytest
from typer.testing import CliRunner

from tests.conftest import FakeZotero, make_collection, make_item
from zelador import cli
from zelador.client import ZoteroClient
from zelador.config import Credentials

runner = CliRunner()

TAXONOMY_YAML = """\
families:
  status: {coloured: true, exclusive: true}
  topic: {}
tags:
  - {tag: "status:read", colour: "#009E73"}
  - {tag: "topic:ai", aliases: [AI]}
"""

REGISTRY_COLORS = [{"name": "status:read", "color": "#009E73"}]


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("ZELADOR_DATA_DIR", str(tmp_path / "data"))
    taxonomy_file = tmp_path / "taxonomy.yaml"
    taxonomy_file.write_text(TAXONOMY_YAML)
    monkeypatch.setenv("ZELADOR_CONFIG_DIR", str(taxonomy_file.parent))
    fake = FakeZotero(
        items=[
            make_item("AAAA1111", version=7, tags=[{"tag": "AI", "type": 1}]),
            make_item("BBBB2222", version=9),
        ],
        collections=[make_collection("COLL1111", "Shelf", version=3)],
        settings={"tagColors": {"value": REGISTRY_COLORS, "version": 40}},
        library_version=42,
        page_size=100,
    )
    creds = Credentials(api_key="sekrit", user_id="11868292")
    monkeypatch.setattr(
        cli,
        "make_client",
        lambda: ZoteroClient(creds, transport=fake.transport, sleep=lambda s: None),
    )
    return fake, tmp_path


def write_changeset(tmp_path, intents, slug="test-change"):
    path = tmp_path / "changeset.json"
    path.write_text(json.dumps({"schema": "changeset.v1", "slug": slug, "intents": intents}))
    return str(path)


MERGE = [{"op": "merge_tag", "from": ["AI"], "into": "topic:ai"}]


def validated_plan(env) -> str:
    """backup + validate, returning the plan id."""
    fake, tmp_path = env
    assert runner.invoke(cli.app, ["backup"]).exit_code == 0
    result = runner.invoke(
        cli.app, ["validate", write_changeset(tmp_path, MERGE), "--json"]
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout.strip().splitlines()[-1])["plan"]


class TestValidate:
    def test_writes_plan_to_data_dir(self, env, tmp_path):
        plan_id = validated_plan(env)
        assert (tmp_path / "data" / "plans" / f"{plan_id}.json").exists()

    def test_without_backup_refused(self, env, tmp_path):
        fake, _ = env
        result = runner.invoke(cli.app, ["validate", write_changeset(tmp_path, MERGE)])
        assert result.exit_code == 1
        assert "backup" in result.output

    def test_validation_failures_exit_2(self, env, tmp_path):
        runner.invoke(cli.app, ["backup"])
        bad = [{"op": "add_tag", "tag": "AI", "keys": ["AAAA1111"]}]  # alias, not canonical
        result = runner.invoke(cli.app, ["validate", write_changeset(tmp_path, bad)])
        assert result.exit_code == 2
        assert "topic:ai" in result.output

    def test_malformed_changeset_exit_2(self, env, tmp_path):
        runner.invoke(cli.app, ["backup"])
        path = tmp_path / "broken.json"
        path.write_text('{"schema": "changeset.v1"}')
        result = runner.invoke(cli.app, ["validate", str(path)])
        assert result.exit_code == 2


class TestApply:
    def test_dry_run_writes_nothing(self, env, tmp_path):
        fake, _ = env
        plan_id = validated_plan(env)
        before = len(fake.requests)
        result = runner.invoke(cli.app, ["apply", plan_id, "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "dry run" in result.output
        assert [r for r in fake.requests[before:] if r.method == "POST"] == []
        assert not list((tmp_path / "data" / "log").glob("*.jsonl"))

    def test_apply_executes_and_logs(self, env, tmp_path):
        fake, _ = env
        plan_id = validated_plan(env)
        result = runner.invoke(cli.app, ["apply", plan_id, "--yes", "--json"])
        assert result.exit_code == 0, result.output
        outcome = json.loads(result.stdout.strip().splitlines()[-1])
        assert outcome["applied"] == 1 and outcome["verified"] is True
        assert fake.items[0]["data"]["tags"] == [{"tag": "topic:ai"}]
        assert (tmp_path / "data" / "log" / f"{plan_id}.jsonl").exists()

    def test_confirmation_declined_aborts(self, env):
        fake, _ = env
        plan_id = validated_plan(env)
        before = len(fake.requests)
        result = runner.invoke(cli.app, ["apply", plan_id], input="n\n")
        assert result.exit_code == 2
        assert [r for r in fake.requests[before:] if r.method == "POST"] == []

    def test_missing_plan_exit_2(self, env):
        runner.invoke(cli.app, ["backup"])
        result = runner.invoke(cli.app, ["apply", "20990101T000000Z-nope", "--yes"])
        assert result.exit_code == 2

    def test_version_conflict_exits_1(self, env):
        fake, _ = env
        plan_id = validated_plan(env)
        fake.items[0]["version"] = 99  # someone else edited the item after validation
        result = runner.invoke(cli.app, ["apply", plan_id, "--yes", "--json"])
        assert result.exit_code == 1
        outcome = json.loads(result.stdout.strip().splitlines()[-1])
        assert outcome["failed"] == 1


class TestUndo:
    def test_undo_reverses_session(self, env):
        fake, _ = env
        plan_id = validated_plan(env)
        runner.invoke(cli.app, ["apply", plan_id, "--yes"])
        assert fake.items[0]["data"]["tags"] == [{"tag": "topic:ai"}]
        result = runner.invoke(cli.app, ["undo", plan_id, "--yes", "--json"])
        assert result.exit_code == 0, result.output
        outcome = json.loads(result.stdout.strip().splitlines()[-1])
        assert outcome["undone"] == 1
        assert fake.items[0]["data"]["tags"] == [{"tag": "AI", "type": 1}]

    def test_conflict_exits_1(self, env):
        fake, _ = env
        plan_id = validated_plan(env)
        runner.invoke(cli.app, ["apply", plan_id, "--yes"])
        fake.items[0]["data"]["tags"] = [{"tag": "hand-edited", "type": 0}]
        fake.items[0]["version"] = 99
        result = runner.invoke(cli.app, ["undo", plan_id, "--yes", "--json"])
        assert result.exit_code == 1
        outcome = json.loads(result.stdout.strip().splitlines()[-1])
        assert outcome["undone"] == 0 and outcome["conflicts"]

    def test_dry_run_writes_nothing(self, env):
        fake, _ = env
        plan_id = validated_plan(env)
        runner.invoke(cli.app, ["apply", plan_id, "--yes"])
        before = len(fake.requests)
        result = runner.invoke(cli.app, ["undo", plan_id, "--dry-run"])
        assert result.exit_code == 0, result.output
        assert [r for r in fake.requests[before:] if r.method in ("POST", "PUT")] == []
        assert fake.items[0]["data"]["tags"] == [{"tag": "topic:ai"}]


class TestReconcile:
    def test_resolves_pending_entries(self, env, tmp_path):
        fake, _ = env
        log_dir = tmp_path / "data" / "log"
        log_dir.mkdir(parents=True)
        operation = {
            "id": "op-001", "group": 0, "op": "add_tag", "kind": "item", "key": "BBBB2222",
            "version": 9, "facet": "tags", "old": [], "new": [{"tag": "topic:ai", "type": 0}],
            "risk": "low",
        }
        (log_dir / "20260601T000000Z-crashed.jsonl").write_text(
            json.dumps({"kind": "header", "schema": "log.v1",
                        "plan": "20260601T000000Z-crashed", "backup": "b",
                        "timestamp": "t"}) + "\n"
            + json.dumps({"kind": "entry", "op": "op-001", "status": "pending",
                          "operation": operation}) + "\n"
        )
        result = runner.invoke(cli.app, ["debug", "reconcile", "20260601T000000Z-crashed"])
        assert result.exit_code == 0, result.output
        assert "0 applied" in result.output and "1 failed" in result.output
        status = runner.invoke(cli.app, ["status", "--json"])
        assert json.loads(status.stdout.strip())["pending_sessions"] == []


class TestRestore:
    def test_restores_named_key_from_backup(self, env, tmp_path):
        fake, _ = env
        runner.invoke(cli.app, ["backup"])
        backup_id = next((tmp_path / "data" / "backups").glob("*.jsonl")).stem
        fake.items[1]["data"]["title"] = "MANGLED"
        fake.items[1]["version"] = 99
        fake.items[1]["data"]["version"] = 99
        result = runner.invoke(
            cli.app, ["debug", "restore", backup_id, "BBBB2222", "--yes", "--json"]
        )
        assert result.exit_code == 0, result.output
        assert fake.items[1]["data"]["title"] == "A title"

    def test_dry_run_previews_without_writing(self, env, tmp_path):
        fake, _ = env
        runner.invoke(cli.app, ["backup"])
        backup_id = next((tmp_path / "data" / "backups").glob("*.jsonl")).stem
        fake.items[1]["data"]["title"] = "MANGLED"
        before = len(fake.requests)
        result = runner.invoke(cli.app, ["debug", "restore", backup_id, "BBBB2222", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert [r for r in fake.requests[before:] if r.method == "POST"] == []
        assert fake.items[1]["data"]["title"] == "MANGLED"

    def test_unknown_key_exit_2(self, env, tmp_path):
        runner.invoke(cli.app, ["backup"])
        backup_id = next((tmp_path / "data" / "backups").glob("*.jsonl")).stem
        result = runner.invoke(cli.app, ["debug", "restore", backup_id, "ZZZZ9999", "--yes"])
        assert result.exit_code == 2
