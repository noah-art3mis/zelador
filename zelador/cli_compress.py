"""Storage commands: zel compress scan/swap/restore.

Registered onto the main Typer app by zelador.cli — thin bodies over the pure
rules in zelador.compress and the file moves in zelador.compress_files.

This is the one command whose writes land on file bytes instead of the Web API,
so `zel undo` cannot reverse it. `restore` is its undo, and it works because
`swap` never overwrites an original — it moves it into the run directory first.
Both directions refuse to move anything whose md5 does not match what the scan
recorded, so a half-copied file is caught rather than installed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from zelador import compress as compress_mod
from zelador import compress_files, config
from zelador.compress import CompressError, report_from_dict, report_to_dict
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


def _load(run: str) -> tuple[Path, compress_mod.Report]:
    """A command argument may be a run id resolved in the data dir, or a path."""
    path = Path(run)
    run_dir = path if path.is_dir() else config.ensure_dir("compress") / run
    report_file = run_dir / "report.json"
    if not report_file.exists():
        note(f"error: no compress run at {run_dir}")
        raise typer.Exit(2)
    try:
        return run_dir, report_from_dict(json.loads(report_file.read_text()))
    except (CompressError, json.JSONDecodeError) as exc:
        note(f"error: {exc}")
        raise typer.Exit(2) from None


def _blocked(blockers: list[str], run: str, as_json: bool) -> None:
    for blocker in blockers:
        note(f"blocked: {blocker}")
    if as_json:
        emit_ndjson({"run": run, "blocked": True, "blockers": blockers})
    raise typer.Exit(1)


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
    candidate is verified against its own original — identical page count, no
    annotations lost, an intact text layer, and a real saving — and rejected
    output is discarded rather than left staged.

    Examples:
        zel compress scan --limit 20
        zel compress scan VVVHR4I4 SHKUM6UJ --preset screen
        zel compress scan --min-bytes 10000000 --json | jq .accepted
    """
    if preset not in compress_files.PRESETS:
        note(f"error: unknown preset {preset!r} — one of {', '.join(compress_files.PRESETS)}")
        raise typer.Exit(2)
    with _cli().guard():
        compress_files.ensure_ghostscript()
        zotero_dir = _zotero_dir()
        found = compress_files.storage_files(zotero_dir, keys)
        candidates = compress_mod.select_candidates(found, min_bytes=min_bytes, limit=limit)
        if keys and not candidates:
            note(f"no PDF candidates among {len(found)} stored file(s) for the given key(s)")
        now = datetime.now(UTC)
        compress_dir = config.ensure_dir("compress")
        run_dir = compress_dir / compress_mod.allocate_run_id(
            now, compress_files.existing_runs(compress_dir)
        )
        run_dir.mkdir(parents=True)
        note(f"{len(candidates)} candidate(s); compressing into {run_dir}")
        report = compress_files.perform_scan(
            zotero_dir,
            run_dir,
            candidates,
            preset=preset,
            min_saving=min_saving,
            text_tolerance=text_tolerance,
            now=now,
            progress=_tick,
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

    Close Zotero first. Refuses the whole run when any storage file has changed
    since the scan, or any staged file does not match the bytes the scan produced.
    Already-swapped entries are skipped, so re-running is safe.

    Examples:
        zel compress swap 20260807T101500Z-compress --dry-run
        zel compress swap 20260807T101500Z-compress --yes --json
    """
    with _cli().guard():
        run_dir, report = _load(run)
        zotero_dir = Path(report.zotero_dir)
        pending = compress_files.pending_entries(report, run_dir)
        blockers = compress_mod.swap_blockers(
            pending,
            compress_files.storage_md5s(pending, zotero_dir),
            compress_files.staged_md5s(pending, run_dir),
        )
        if blockers:
            _blocked(blockers, report.run, as_json)
        reclaimed = sum(e.before.bytes - e.after.bytes for e in pending)
        if dry_run:
            for entry in pending:
                note(f"would install {entry.label} — {entry.reason}")
            _finish(
                as_json,
                {
                    "run": report.run,
                    "dry_run": True,
                    "swapped": 0,
                    "pending": len(pending),
                    "reclaimed_bytes": reclaimed,
                },
                [f"{len(pending)} file(s) would move, {_mb(reclaimed)} reclaimed"],
            )
            return
        if pending and not yes:
            typer.confirm(
                f"Install {len(pending)} compressed file(s) into {zotero_dir}? "
                "Zotero must be closed.",
                abort=True,
            )
        moved = compress_files.perform_swap(pending, run_dir, zotero_dir)
        _finish(
            as_json,
            {
                "run": report.run,
                "swapped": len(moved),
                "reclaimed_bytes": reclaimed,
                "originals": str(run_dir / "originals"),
            },
            [f"installed {e.label} — {e.reason}" for e in moved]
            + [
                f"{len(moved)} file(s) installed, {_mb(reclaimed)} reclaimed",
                f"originals quarantined in {run_dir / 'originals'}",
                f"undo with: zel compress restore {report.run}",
            ],
        )


def restore(
    run: Annotated[str, typer.Argument(help="Run id (resolved in <data dir>/compress/) or path.")],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print what would move, touch nothing.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation gate.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Final outcome object.")] = False,
):
    """Move a run's quarantined originals back into storage — the undo for swap.

    The compressed files return to the run's staging area, so a restored run can
    be swapped again without re-scanning. Refuses when a quarantined original is
    incomplete, or when the file it would displace is not the one this run
    installed — another run having swapped it since is not something to overwrite.

    Examples:
        zel compress restore 20260807T101500Z-compress --dry-run
        zel compress restore 20260807T101500Z-compress --yes --json
    """
    with _cli().guard():
        run_dir, report = _load(run)
        zotero_dir = Path(report.zotero_dir)
        swapped = compress_files.swapped_entries(report, run_dir)
        blockers = compress_mod.restore_blockers(
            swapped,
            compress_files.storage_md5s(swapped, zotero_dir),
            compress_files.quarantine_md5s(swapped, run_dir),
        )
        if blockers:
            _blocked(blockers, report.run, as_json)
        if dry_run:
            for entry in swapped:
                note(f"would restore {entry.label}")
            _finish(
                as_json,
                {"run": report.run, "dry_run": True, "restored": 0, "pending": len(swapped)},
                [f"{len(swapped)} original(s) would move"],
            )
            return
        if swapped and not yes:
            typer.confirm(
                f"Restore {len(swapped)} original(s) into {zotero_dir}? Zotero must be closed.",
                abort=True,
            )
        restored = compress_files.perform_restore(swapped, run_dir, zotero_dir)
        _finish(
            as_json,
            {"run": report.run, "restored": len(restored)},
            [f"restored {e.label}" for e in restored]
            + [f"{len(restored)} original(s) restored"],
        )


def _finish(as_json: bool, outcome: dict, lines: list[str]) -> None:
    if as_json:
        emit_ndjson(outcome)
    else:
        for line in lines:
            print(line)


def _mb(n: int) -> str:
    return f"{n / 1048576:.1f} MB"


def _tick(entry) -> None:
    note(f"  {entry.label}: {entry.reason}")
