"""`zel compress scan/swap/restore` end to end, against real PDFs and a stubbed Ghostscript."""

from __future__ import annotations

import json

import pytest
from pypdf import PdfReader, PdfWriter
from typer.testing import CliRunner

from zelador import cli, compress_files, config

runner = CliRunner()

BIG_PADDING = "x" * 60000


def write_pdf(path, pages=3, padding=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    if padding:
        writer.add_metadata({"/Comment": padding})
    with path.open("wb") as fh:
        writer.write(fh)
    return path


def shrink(src, dst, preset):
    """Stand-in for Ghostscript: same pages, padding dropped."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    for page in PdfReader(src).pages:
        writer.add_page(page)
    with dst.open("wb") as fh:
        writer.write(fh)


def grow(src, dst, preset):
    """A compressor that made things worse — the case that must be rejected."""
    write_pdf(dst, pages=len(PdfReader(src).pages), padding=BIG_PADDING * 2)


def drop_a_page(src, dst, preset):
    write_pdf(dst, pages=len(PdfReader(src).pages) - 1)


@pytest.fixture
def env(monkeypatch, tmp_path):
    zotero = tmp_path / "Zotero"
    (zotero / "storage").mkdir(parents=True)
    write_pdf(zotero / "storage" / "AAAAAAAA" / "book.pdf", pages=3, padding=BIG_PADDING)
    config_file = tmp_path / "config.yaml"
    config_file.write_text(f"zotero_data_dir: {zotero}\n")
    monkeypatch.setattr(config, "CONFIG_FILE", config_file)
    monkeypatch.setenv("ZELADOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(compress_files.shutil, "which", lambda _: "/usr/bin/gs")
    monkeypatch.setattr(compress_files, "compress_pdf", shrink)
    return tmp_path, zotero


def scan(*args):
    result = runner.invoke(cli.app, ["compress", "scan", "--min-bytes", "0", "--json", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout.strip().splitlines()[-1])


def stored(zotero):
    return (zotero / "storage" / "AAAAAAAA" / "book.pdf").read_bytes()


class TestScan:
    def test_accepts_a_real_saving_and_stages_the_file(self, env):
        tmp_path, zotero = env
        out = scan()
        assert out["accepted"] == 1
        run_dir = tmp_path / "data" / "compress" / out["run"]
        assert (run_dir / "staged" / "AAAAAAAA" / "book.pdf").exists()
        assert json.loads((run_dir / "report.json").read_text())["contract"] == "compress.v1"

    def test_leaves_the_library_untouched(self, env):
        tmp_path, zotero = env
        before = stored(zotero)
        scan()
        assert stored(zotero) == before

    def test_rejects_a_file_that_grew(self, env, monkeypatch):
        monkeypatch.setattr(compress_files, "compress_pdf", grow)
        out = scan()
        assert out["accepted"] == 0 and out["candidates"] == 1

    def test_rejects_a_lost_page(self, env, monkeypatch):
        tmp_path, zotero = env
        monkeypatch.setattr(compress_files, "compress_pdf", drop_a_page)
        out = scan()
        assert out["accepted"] == 0
        report = json.loads(
            (tmp_path / "data" / "compress" / out["run"] / "report.json").read_text()
        )
        assert "pages changed: 3 -> 2" in report["entries"][0]["reason"]

    def test_missing_ghostscript_fails_loudly(self, env, monkeypatch):
        monkeypatch.setattr(compress_files.shutil, "which", lambda _: None)
        result = runner.invoke(cli.app, ["compress", "scan", "--min-bytes", "0"])
        assert result.exit_code == 1 and "ghostscript" in result.output.lower()

    def test_no_candidates_is_not_a_failure(self, env):
        result = runner.invoke(
            cli.app, ["compress", "scan", "--min-bytes", "999999999", "--json"]
        )
        assert result.exit_code == 0
        assert json.loads(result.stdout.strip().splitlines()[-1])["candidates"] == 0


class TestSwap:
    def test_dry_run_moves_nothing(self, env):
        tmp_path, zotero = env
        out = scan()
        before = stored(zotero)
        result = runner.invoke(cli.app, ["compress", "swap", out["run"], "--dry-run"])
        assert result.exit_code == 0
        assert stored(zotero) == before
        assert not (tmp_path / "data" / "compress" / out["run"] / "originals").exists()

    def test_installs_the_compressed_file_and_quarantines_the_original(self, env):
        tmp_path, zotero = env
        original = stored(zotero)
        out = scan()
        staged = (
            tmp_path / "data" / "compress" / out["run"] / "staged" / "AAAAAAAA" / "book.pdf"
        ).read_bytes()
        result = runner.invoke(cli.app, ["compress", "swap", out["run"], "--yes"])
        assert result.exit_code == 0, result.output
        assert stored(zotero) == staged
        quarantined = (
            tmp_path / "data" / "compress" / out["run"] / "originals" / "AAAAAAAA" / "book.pdf"
        )
        assert quarantined.read_bytes() == original

    def test_swapping_twice_changes_nothing_further(self, env):
        tmp_path, zotero = env
        original = stored(zotero)
        out = scan()
        runner.invoke(cli.app, ["compress", "swap", out["run"], "--yes"])
        after_first = stored(zotero)
        result = runner.invoke(cli.app, ["compress", "swap", out["run"], "--yes", "--json"])
        assert result.exit_code == 0
        assert stored(zotero) == after_first
        quarantined = (
            tmp_path / "data" / "compress" / out["run"] / "originals" / "AAAAAAAA" / "book.pdf"
        )
        assert quarantined.read_bytes() == original

    def test_refuses_when_the_storage_file_changed_since_the_scan(self, env):
        tmp_path, zotero = env
        out = scan()
        write_pdf(zotero / "storage" / "AAAAAAAA" / "book.pdf", pages=3, padding="different")
        result = runner.invoke(cli.app, ["compress", "swap", out["run"], "--yes"])
        assert result.exit_code == 1
        assert "AAAAAAAA" in result.output and "changed" in result.output

    def test_unknown_run_is_bad_input(self, env):
        result = runner.invoke(cli.app, ["compress", "swap", "20260101T000000Z-compress", "--yes"])
        assert result.exit_code == 2


class TestRestore:
    def test_puts_the_originals_back_byte_for_byte(self, env):
        tmp_path, zotero = env
        original = stored(zotero)
        out = scan()
        runner.invoke(cli.app, ["compress", "swap", out["run"], "--yes"])
        assert stored(zotero) != original
        result = runner.invoke(cli.app, ["compress", "restore", out["run"], "--yes"])
        assert result.exit_code == 0, result.output
        assert stored(zotero) == original

    def test_restoring_an_unswapped_run_is_a_no_op(self, env):
        tmp_path, zotero = env
        out = scan()
        before = stored(zotero)
        result = runner.invoke(cli.app, ["compress", "restore", out["run"], "--yes", "--json"])
        assert result.exit_code == 0
        assert stored(zotero) == before
        assert json.loads(result.stdout.strip().splitlines()[-1])["restored"] == 0
