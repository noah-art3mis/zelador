"""Storage commands: zel compress scan/swap/restore.

Registered onto the main Typer app by zelador.cli — thin bodies over the pure
rules in zelador.compress and the file moves in zelador.compress_files.

This is the one command whose writes land on file bytes instead of the Web API,
so `zel undo` cannot reverse it. `restore` is its undo, and it works because
`swap` never overwrites an original — it moves it into the run directory first.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from zelador import compress as compress_mod
from zelador import compress_files, config
from zelador.compress import CompressError, report_from_dict, report_to_dict, swap_blockers
from zelador.output import emit_ndjson, note

compress_app = typer.Typer(
    help="Reclaim PDF bytes no reader sees. Scan measures, swap installs, restore reverses.",
    no_args_is_help=True,
)


def _cli():
    from zelador import cli

    return cli


def register(app: typer.Typer) -> None:
    app.add_typer(compress_app, name="compress", rich_help_panel="Storage")
    compress_app.command()(scan)
    compress_app.command()(swap)
    compress_app.command()(restore)


def _zotero_dir() -> Path:
    # CONFIG_FILE is passed explicitly: load_config binds it as a default at import
    # time, so a caller that repoints the module attribute would otherwise be ignored.
    return config.discover_zotero_dir(config.load_config(config.CONFIG_FILE).zotero_data_dir)


def _run_dir(run: str) -> Path:
    """A command argument may be a run id resolved in the data dir, or a path."""
    path = Path(run)
    return path if path.is_dir() else config.ensure_dir("compress") / run


def _load(run: str) -> tuple[Path, compress_mod.Report]:
    run_dir = _run_dir(run)
    report_file = run_dir / "report.json"
    if not report_file.exists():
        note(f"error: no compress run at {run_dir}")
        raise typer.Exit(2)
    try:
        return run_dir, report_from_dict(json.loads(report_file.read_text()))
    except (CompressError, json.JSONDecodeError) as exc:
        note(f"error: {exc}")
        raise typer.Exit(2) from None


def scan(
    keys: Annotated[
        list[str] | None, typer.Argument(help="Attachment keys; default is the whole library.")
    ] = None,
    preset: Annotated[
        str, typer.Option("--preset", help="Ghostscript quality: screen|ebook|printer|prepress.")
    ] = "ebook",
    min_bytes: Annotated[
        int, typer.Option("--min-bytes", help="Ignore files smaller than this.")
    ] = 5_000_000,
    min_saving: Annotated[
        float, typer.Option("--min-saving", help="Reject savings below this fraction.")
    ] = 0.25,
    text_tolerance: Annotated[
        float, typer.Option("--text-tolerance", help="Allowed shrink of the extracted text.")
    ] = 0.02,
    limit: Annotated[int | None, typer.Option("--limit", help="Only the N largest.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Final outcome object.")] = False,
):
    """Compress candidates into a run directory and report what each one would cost.

    Touches nothing in the library: originals are read, never written. Every
    candidate is verified against its own original — identical page count, an
    intact text layer, and a real saving — and rejected output is discarded.

    Examples:
        zel compress scan --limit 20
        zel compress scan VVVHR4I4 SHKUM6UJ --preset screen
        zel compress scan --min-bytes 10000000 --json | jq .accepted
    """
    with _cli().guard():
        compress_files.ensure_ghostscript()
        zotero_dir = _zotero_dir()
        candidates = compress_mod.select_candidates(
            compress_files.storage_files(zotero_dir, keys), min_bytes=min_bytes, limit=limit
        )
        now = datetime.now(UTC)
        run_dir = config.ensure_dir("compress") / compress_mod.run_id(now)
        run_dir.mkdir(parents=True, exist_ok=True)
        if not as_json:
            note(f"{len(candidates)} candidate(s); compressing into {run_dir}")
        report = compress_files.perform_scan(
            zotero_dir,
            run_dir,
            candidates,
            preset=preset,
            min_saving=min_saving,
            text_tolerance=text_tolerance,
            now=now,
            progress=None if as_json else _tick,
        )
        report_file = run_dir / "report.json"
        report_file.write_text(json.dumps(report_to_dict(report), indent=2))
        kept = compress_mod.accepted(report)
        if as_json:
            emit_ndjson(
                {
                    "run": report.run,
                    "path": str(run_dir),
                    "candidates": len(report.entries),
                    "accepted": len(kept),
                    "before_bytes": sum(e.before.bytes for e in kept),
                    "after_bytes": sum(e.after.bytes for e in kept),
                }
            )
        else:
            for line in compress_mod.summarize(report):
                print(line)
            print(f"report written: {report_file}")
            if kept:
                print(f"install with: zel compress swap {report.run} --dry-run")


def swap(
    run: Annotated[str, typer.Argument(help="Run id (resolved in <data dir>/compress/) or path.")],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print what would move, touch nothing.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation gate.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Final outcome object.")] = False,
):
    """Install a run's accepted files, quarantining each original inside the run.

    Close Zotero first. Refuses when a storage file has changed since the scan —
    that means the staged copy was built from bytes that no longer exist.
    Already-swapped entries are skipped, so re-running is safe.

    Examples:
        zel compress swap 20260807T101500Z-compress --dry-run
        zel compress swap 20260807T101500Z-compress --yes --json
    """
    with _cli().guard():
        run_dir, report = _load(run)
        zotero_dir = Path(report.zotero_dir)
        pending = compress_files.pending_entries(report, run_dir)
        blockers = swap_blockers(
            compress_mod.with_entries(report, pending),
            compress_files.current_md5s(pending, zotero_dir),
            compress_files.staged_present(pending, run_dir),
        )
        if blockers:
            for blocker in blockers:
                note(f"blocked: {blocker}")
            raise typer.Exit(1)
        reclaimed = sum(e.before.bytes - e.after.bytes for e in pending)
        if dry_run:
            for entry in pending:
                print(f"would install {entry.key}/{entry.filename} — {entry.reason}")
            print(f"{len(pending)} file(s), {reclaimed / 1048576:.1f} MB reclaimed")
            return
        if pending and not yes:
            typer.confirm(
                f"Install {len(pending)} compressed file(s) into {zotero_dir}? "
                "Zotero must be closed.",
                abort=True,
            )
        moved = compress_files.perform_swap(pending, run_dir, zotero_dir)
        if as_json:
            emit_ndjson(
                {
                    "run": report.run,
                    "swapped": len(moved),
                    "reclaimed_bytes": reclaimed,
                    "originals": str(run_dir / "originals"),
                }
            )
        else:
            for entry in moved:
                print(f"installed {entry.key}/{entry.filename} — {entry.reason}")
            print(f"{len(moved)} file(s) installed, {reclaimed / 1048576:.1f} MB reclaimed")
            print(f"originals quarantined in {run_dir / 'originals'}")
            print(f"undo with: zel compress restore {report.run}")


def restore(
    run: Annotated[str, typer.Argument(help="Run id (resolved in <data dir>/compress/) or path.")],
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation gate.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Final outcome object.")] = False,
):
    """Move a run's quarantined originals back into storage — the undo for swap.

    The compressed files return to the run's staging area, so a restored run can
    be swapped again without re-scanning.

    Examples:
        zel compress restore 20260807T101500Z-compress
        zel compress restore 20260807T101500Z-compress --yes --json
    """
    with _cli().guard():
        run_dir, report = _load(run)
        zotero_dir = Path(report.zotero_dir)
        swapped = compress_files.swapped_entries(report, run_dir)
        if swapped and not yes:
            typer.confirm(
                f"Restore {len(swapped)} original(s) into {zotero_dir}? Zotero must be closed.",
                abort=True,
            )
        restored = compress_files.perform_restore(report, run_dir, zotero_dir)
        if as_json:
            emit_ndjson({"run": report.run, "restored": len(restored)})
        else:
            for entry in restored:
                print(f"restored {entry.key}/{entry.filename}")
            print(f"{len(restored)} original(s) restored")


def _tick(entry) -> None:
    note(f"  {entry.key}/{entry.filename}: {entry.reason}")
