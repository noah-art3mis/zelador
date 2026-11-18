"""CLI tests for the read commands: zel items / tags / collections / debug."""

import json

import pytest
from typer.testing import CliRunner

from tests.conftest import FakeZotero, make_collection, make_item, make_tag
from zelador import cli
from zelador.client import ZoteroClient
from zelador.config import Credentials

runner = CliRunner()


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setenv("ZELADOR_DATA_DIR", str(tmp_path / "data"))
    fake = FakeZotero(
        items=[
            make_item("AAAA1111", version=3, title="On Testing", date="2021"),
            make_item("BBBB2222", version=8, item_type="book", title="A Book", date="2019"),
        ],
        collections=[
            make_collection("CCCC0001", "projects"),
            make_collection("CCCC0002", "capstone", parent="CCCC0001"),
        ],
        tags=[make_tag("status:read", num_items=12), make_tag("Machine Learning", tag_type=1)],
        page_size=100,
    )
    creds = Credentials(api_key="sekrit", user_id="11868292")

    def fake_client():
        return ZoteroClient(creds, transport=fake.transport, sleep=lambda s: None)

    monkeypatch.setattr(cli, "make_client", fake_client)
    return fake


class TestItems:
    def test_dump_human(self, fake):
        result = runner.invoke(cli.app, ["items"])
        assert result.exit_code == 0
        assert "AAAA1111" in result.output
        assert "On Testing" in result.output

    def test_dump_json_is_ndjson(self, fake):
        result = runner.invoke(cli.app, ["items", "--json"])
        assert result.exit_code == 0
        lines = [json.loads(line) for line in result.output.strip().splitlines()]
        assert [obj["key"] for obj in lines] == ["AAAA1111", "BBBB2222"]

    def test_batch_read_by_keys(self, fake):
        result = runner.invoke(cli.app, ["items", "BBBB2222", "--json"])
        assert result.exit_code == 0
        lines = [json.loads(line) for line in result.output.strip().splitlines()]
        assert [obj["key"] for obj in lines] == ["BBBB2222"]
        assert any("itemKey" in str(r.url) for r in fake.requests)

    def test_since_passed_through(self, fake):
        result = runner.invoke(cli.app, ["items", "--since", "5", "--json"])
        assert result.exit_code == 0
        lines = [json.loads(line) for line in result.output.strip().splitlines()]
        assert [obj["key"] for obj in lines] == ["BBBB2222"]

    def test_bib_renders_entries(self, fake):
        result = runner.invoke(cli.app, ["items", "--bib"])
        assert result.exit_code == 0
        assert "On Testing" in result.output
        assert "csl-bib-body" not in result.output  # HTML stripped for humans
        bib_requests = [r for r in fake.requests if "include=bib" in str(r.url)]
        assert bib_requests and all("style=apa" in str(r.url) for r in bib_requests)


class TestTags:
    def test_human_output_shows_counts_and_type(self, fake):
        result = runner.invoke(cli.app, ["tags"])
        assert result.exit_code == 0
        assert "status:read" in result.output
        assert "12" in result.output
        assert "auto" in result.output  # Machine Learning is an automatic tag

    def test_json(self, fake):
        result = runner.invoke(cli.app, ["tags", "--json"])
        lines = [json.loads(line) for line in result.output.strip().splitlines()]
        assert lines[0]["tag"] == "status:read"


class TestCollections:
    def test_tree_indents_children(self, fake):
        result = runner.invoke(cli.app, ["collections"])
        assert result.exit_code == 0
        lines = result.output.splitlines()
        parent_line = next(line for line in lines if "projects" in line)
        child_line = next(line for line in lines if "capstone" in line)
        assert len(child_line) - len(child_line.lstrip()) > len(parent_line) - len(
            parent_line.lstrip()
        )

    def test_json(self, fake):
        result = runner.invoke(cli.app, ["collections", "--json"])
        lines = [json.loads(line) for line in result.output.strip().splitlines()]
        assert {obj["key"] for obj in lines} == {"CCCC0001", "CCCC0002"}

    def test_orphaned_subtree_rendered_not_dropped(self):
        orphan = make_collection("DDDD0001", "lost-parent", parent="GONE9999")
        child = make_collection("DDDD0002", "lost-child", parent="DDDD0001")
        lines = cli.render_collection_tree([orphan, child])
        assert any("lost-parent" in line and "orphaned" in line for line in lines)
        assert any("lost-child" in line for line in lines)


class TestDebug:
    def test_whoami(self, fake):
        result = runner.invoke(cli.app, ["debug", "whoami"])
        assert result.exit_code == 0
        assert "noah-art3mis" in result.output
        assert "11868292" in result.output

    def test_paths_shows_data_dir(self, fake, tmp_path):
        result = runner.invoke(cli.app, ["debug", "paths"])
        assert result.exit_code == 0
        assert str(tmp_path / "data") in result.output

    def test_paths_shows_config_dir_and_file_presence(self, fake, isolated_config_dir):
        isolated_config_dir.mkdir()
        (isolated_config_dir / "taxonomy.yaml").write_text("families: {}\n")
        result = runner.invoke(cli.app, ["debug", "paths"])
        assert f"config dir:    {isolated_config_dir}" in result.output
        assert f"{isolated_config_dir / 'taxonomy.yaml'} (present)" in result.output
        assert f"{isolated_config_dir / 'config.yaml'} (absent)" in result.output

    def test_probe_prints_raw_json(self, fake):
        result = runner.invoke(cli.app, ["debug", "probe", "tags"])
        assert result.exit_code == 0
        assert "status:read" in result.output


class TestErrors:
    def test_api_failure_exits_1(self, fake):
        import httpx

        fake.script.append(httpx.Response(500, text="boom"))
        result = runner.invoke(cli.app, ["items"])
        assert result.exit_code == 1

    def test_missing_credentials_exit_1(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ZELADOR_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
        monkeypatch.delenv("ZOTERO_USER_ID", raising=False)
        result = runner.invoke(cli.app, ["items"])
        assert result.exit_code == 1
