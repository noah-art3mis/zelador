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


def partial_shrink(remaining):
    """A compressor that only gets part way, so a later run can shrink the file again."""

    def compress(src, dst, preset):
        write_pdf(dst, pages=len(PdfReader(src).pages), padding="x" * remaining)

    return compress


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


def outcome(result):
    return json.loads(result.stdout.strip().splitlines()[-1])


def stored(zotero, key="AAAAAAAA", name="book.pdf"):
    return (zotero / "storage" / key / name).read_bytes()


def run_path(tmp_path, out):
    return tmp_path / "data" / "compress" / out["run"]


class TestScan:
    def test_accepts_a_real_saving_and_stages_the_file(self, env):
        tmp_path, zotero = env
        out = scan()
        assert out["accepted"] == 1
        run_dir = run_path(tmp_path, out)
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
        report = json.loads((run_path(tmp_path, out) / "report.json").read_text())
        assert "pages changed: 3 -> 2" in report["entries"][0]["reason"]

    def test_an_unreadable_pdf_does_not_abort_the_run(self, env, tmp_path):
        """One corrupt file must not discard the work already done on every other."""
        _, zotero = env
        (zotero / "storage" / "ZZZZZZZZ").mkdir()
        (zotero / "storage" / "ZZZZZZZZ" / "broken.pdf").write_bytes(b"not a pdf at all")
        out = scan()
        assert out["candidates"] == 2 and out["accepted"] == 1
        report = json.loads((run_path(tmp_path, out) / "report.json").read_text())
        broken = [e for e in report["entries"] if e["key"] == "ZZZZZZZZ"][0]
        assert not broken["accepted"] and "could not read" in broken["reason"]

    def test_missing_ghostscript_fails_loudly(self, env, monkeypatch):
        monkeypatch.setattr(compress_files.shutil, "which", lambda _: None)
        result = runner.invoke(cli.app, ["compress", "scan", "--min-bytes", "0"])
        assert result.exit_code == 1 and "ghostscript" in result.output.lower()

    def test_unknown_preset_is_bad_input(self, env):
        """Exit 0 with everything 'rejected' would read as 'nothing worth compressing'."""
        result = runner.invoke(
            cli.app, ["compress", "scan", "--min-bytes", "0", "--preset", "bogus"]
        )
        assert result.exit_code == 2 and "bogus" in result.output

    def test_no_candidates_is_not_a_failure(self, env):
        result = runner.invoke(cli.app, ["compress", "scan", "--min-bytes", "999999999", "--json"])
        assert result.exit_code == 0
        assert outcome(result)["candidates"] == 0

    def test_two_scans_in_the_same_second_get_separate_runs(self, env, monkeypatch):
        """Sharing a directory would let the second report orphan the first's originals."""
        first = scan()
        second = scan()
        assert first["run"] != second["run"]


class TestSwap:
    def test_dry_run_moves_nothing_and_still_answers_in_json(self, env, tmp_path):
        _, zotero = env
        out = scan()
        before = stored(zotero)
        result = runner.invoke(cli.app, ["compress", "swap", out["run"], "--dry-run", "--json"])
        assert result.exit_code == 0
        assert outcome(result) == {
            "run": out["run"],
            "dry_run": True,
            "swapped": 0,
            "pending": 1,
            "reclaimed_bytes": out["before_bytes"] - out["after_bytes"],
        }
        assert stored(zotero) == before
        assert not (run_path(tmp_path, out) / "originals").exists()

    def test_installs_the_compressed_file_and_quarantines_the_original(self, env, tmp_path):
        _, zotero = env
        original = stored(zotero)
        out = scan()
        staged = (run_path(tmp_path, out) / "staged" / "AAAAAAAA" / "book.pdf").read_bytes()
        result = runner.invoke(cli.app, ["compress", "swap", out["run"], "--yes"])
        assert result.exit_code == 0, result.output
        assert stored(zotero) == staged
        quarantined = run_path(tmp_path, out) / "originals" / "AAAAAAAA" / "book.pdf"
        assert quarantined.read_bytes() == original

    def test_swapping_twice_changes_nothing_further(self, env, tmp_path):
        _, zotero = env
        original = stored(zotero)
        out = scan()
        runner.invoke(cli.app, ["compress", "swap", out["run"], "--yes"])
        after_first = stored(zotero)
        result = runner.invoke(cli.app, ["compress", "swap", out["run"], "--yes", "--json"])
        assert result.exit_code == 0
        assert stored(zotero) == after_first
        quarantined = run_path(tmp_path, out) / "originals" / "AAAAAAAA" / "book.pdf"
        assert quarantined.read_bytes() == original

    def test_refuses_when_the_storage_file_changed_since_the_scan(self, env):
        _, zotero = env
        out = scan()
        write_pdf(zotero / "storage" / "AAAAAAAA" / "book.pdf", pages=3, padding="different")
        result = runner.invoke(cli.app, ["compress", "swap", out["run"], "--yes"])
        assert result.exit_code == 1
        assert "AAAAAAAA/book.pdf" in result.output and "changed" in result.output

    def test_refuses_a_truncated_staged_file(self, env, tmp_path):
        """It exists — but a move that died mid-copy leaves exactly this."""
        _, zotero = env
        original = stored(zotero)
        out = scan()
        staged = run_path(tmp_path, out) / "staged" / "AAAAAAAA" / "book.pdf"
        staged.write_bytes(staged.read_bytes()[:200])
        result = runner.invoke(cli.app, ["compress", "swap", out["run"], "--yes"])
        assert result.exit_code == 1 and "damaged" in result.output
        assert stored(zotero) == original

    def test_two_pdfs_under_one_key_both_swap(self, env, tmp_path):
        """key alone is not an identity: hashes keyed by it shadow each other."""
        _, zotero = env
        write_pdf(zotero / "storage" / "AAAAAAAA" / "extra.pdf", pages=2, padding=BIG_PADDING)
        out = scan()
        assert out["accepted"] == 2
        result = runner.invoke(cli.app, ["compress", "swap", out["run"], "--yes", "--json"])
        assert result.exit_code == 0, result.output
        assert outcome(result)["swapped"] == 2

    def test_a_failing_move_is_reported_not_raised(self, env, monkeypatch):
        """guard() promises a reason on stderr, never a traceback."""

        def boom(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        out = scan()
        monkeypatch.setattr(compress_files.shutil, "move", boom)
        result = runner.invoke(cli.app, ["compress", "swap", out["run"], "--yes"])
        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "No space left" in result.output

    def test_unknown_run_is_bad_input(self, env):
        result = runner.invoke(cli.app, ["compress", "swap", "20260101T000000Z-compress", "--yes"])
        assert result.exit_code == 2


class TestRestore:
    def test_puts_the_originals_back_byte_for_byte(self, env):
        _, zotero = env
        original = stored(zotero)
        out = scan()
        runner.invoke(cli.app, ["compress", "swap", out["run"], "--yes"])
        assert stored(zotero) != original
        result = runner.invoke(cli.app, ["compress", "restore", out["run"], "--yes"])
        assert result.exit_code == 0, result.output
        assert stored(zotero) == original

    def test_restoring_an_unswapped_run_is_a_no_op(self, env):
        _, zotero = env
        out = scan()
        before = stored(zotero)
        result = runner.invoke(cli.app, ["compress", "restore", out["run"], "--yes", "--json"])
        assert result.exit_code == 0
        assert stored(zotero) == before
        assert outcome(result)["restored"] == 0

    def test_dry_run_moves_nothing(self, env):
        _, zotero = env
        original = stored(zotero)
        out = scan()
        runner.invoke(cli.app, ["compress", "swap", out["run"], "--yes"])
        compressed = stored(zotero)
        result = runner.invoke(cli.app, ["compress", "restore", out["run"], "--dry-run", "--json"])
        assert result.exit_code == 0
        assert outcome(result)["pending"] == 1
        assert stored(zotero) == compressed
        assert stored(zotero) != original

    def test_refuses_a_partial_quarantined_original(self, env, tmp_path):
        """The fragment must not be installed over an intact library file."""
        _, zotero = env
        out = scan()
        runner.invoke(cli.app, ["compress", "swap", out["run"], "--yes"])
        compressed = stored(zotero)
        quarantined = run_path(tmp_path, out) / "originals" / "AAAAAAAA" / "book.pdf"
        quarantined.write_bytes(quarantined.read_bytes()[:300])
        result = runner.invoke(cli.app, ["compress", "restore", out["run"], "--yes"])
        assert result.exit_code == 1 and "damaged" in result.output
        assert stored(zotero) == compressed

    def test_refuses_when_a_later_run_swapped_the_same_file(self, env, monkeypatch):
        """Restoring run one now would hand run two's compressed file back as an original."""
        _, zotero = env
        original = stored(zotero)
        monkeypatch.setattr(compress_files, "compress_pdf", partial_shrink(30000))
        first = scan()
        runner.invoke(cli.app, ["compress", "swap", first["run"], "--yes"])
        monkeypatch.setattr(compress_files, "compress_pdf", partial_shrink(0))
        second = scan()
        assert second["accepted"] == 1
        runner.invoke(cli.app, ["compress", "swap", second["run"], "--yes"])
        result = runner.invoke(cli.app, ["compress", "restore", first["run"], "--yes"])
        assert result.exit_code == 1 and "another run" in result.output
        result = runner.invoke(cli.app, ["compress", "restore", second["run"], "--yes"])
        assert result.exit_code == 0, result.output
        result = runner.invoke(cli.app, ["compress", "restore", first["run"], "--yes"])
        assert result.exit_code == 0, result.output
        assert stored(zotero) == original
