"""Change-loop commands: zel validate/apply/undo, zel debug reconcile/restore.

Registered onto the main Typer app by zelador.cli — thin bodies over the
zelador.write machinery, same output contract as every other command.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from zelador import backup as backup_mod
from zelador import config
from zelador import taxonomy as taxonomy_mod
from zelador.output import emit_ndjson, note
from zelador.write import apply as apply_mod
from zelador.write import recover as recover_mod
from zelador.write import undo as undo_mod
from zelador.write.contracts import ChangesetError, load_changeset, load_plan, save_plan
from zelador.write.expand import ValidationError, expand


def _cli():
    from zelador import cli

    return cli


def _resolve(ref: str, directory: Path, suffix: str) -> Path:
    """A command argument may be a file path or an id resolved in the data dir."""
    path = Path(ref)
    return path if path.exists() else directory / f"{ref}{suffix}"


def register(app: typer.Typer, debug_app: typer.Typer) -> None:
    app.command(rich_help_panel="Change loop")(validate)
    app.command(rich_help_panel="Change loop")(apply)
    app.command(rich_help_panel="Change loop")(undo)
    debug_app.command()(reconcile)
    debug_app.command()(restore)


def validate(
    changeset: Annotated[str, typer.Argument(help="Path to a changeset.v1 JSON file.")],
    as_json: Annotated[bool, typer.Option("--json", help="Final outcome object.")] = False,
):
    """Check a changeset against taxonomy + hard rules; expand into a version-pinned plan.

    The plan lands in <data dir>/plans/ and is what you approve and apply.
    Validation failures list every problem at once and nothing expands.

    Examples:
        zel validate changesets/merge-ai-tags.json
        zel validate changesets/merge-ai-tags.json --json | jq .plan
    """
    with _cli().guard():
        try:
            parsed = load_changeset(Path(changeset))
        except ChangesetError as exc:
            note(f"error: {exc}")
            raise typer.Exit(2) from None
        info = backup_mod.latest_backup(config.ensure_dir("backups"))
        if info is None:
            note("error: no backup exists — run `zel backup` first")
            raise typer.Exit(1)
        registry = (
            taxonomy_mod.load_taxonomy()
            if config.taxonomy_file().exists()
            else None
        )
        try:
            plan = expand(
                parsed, _cli().make_client(), registry, backup=info.timestamp, now=datetime.now(UTC)
            )
        except ValidationError as exc:
            for failure in exc.failures:
                note(f"invalid: {failure}")
            if as_json:
                emit_ndjson({"valid": False, "failures": exc.failures})
            raise typer.Exit(2) from None
        path = save_plan(plan, config.ensure_dir("plans"))
        if as_json:
            emit_ndjson(
                {
                    "valid": True,
                    "plan": plan.id,
                    "path": str(path),
                    "operations": len(plan.operations),
                    "groups": len(plan.intents),
                    "settings": plan.settings is not None,
                    "library_version": plan.library_version,
                }
            )
        else:
            for line in apply_mod.summarize(plan):
                print(line)
            print(f"plan written: {path}")


def apply(
    plan: Annotated[str, typer.Argument(help="Plan id (resolved in <data dir>/plans/) or path.")],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the writes, touch nothing.")
    ] = False,
    big: Annotated[bool, typer.Option("--big", help="Allow plans touching >200 objects.")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation gate.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Final outcome object.")] = False,
):
    """Execute an expanded plan through the write-ahead change log.

    Refuses unless the plan's pinned backup exists and no session has
    unresolved pending entries. Always dry-run first.

    Examples:
        zel apply 20260719T120000Z-merge-ai-tags --dry-run
        zel apply 20260719T120000Z-merge-ai-tags --yes --json
    """
    with _cli().guard():
        path = _resolve(plan, config.ensure_dir("plans"), ".json")
        try:
            loaded = load_plan(path)
        except ChangesetError as exc:
            note(f"error: {exc}")
            raise typer.Exit(2) from None
        backups_dir = config.ensure_dir("backups")
        log_dir = config.ensure_dir("log")
        try:
            apply_mod.check_preconditions(loaded, backups_dir, log_dir, big)
        except apply_mod.ApplyRefused as exc:
            note(f"refused: {exc}")
            raise typer.Exit(1) from None
        for line in apply_mod.summarize(loaded):
            note(line)
        if dry_run:
            note("dry run — nothing written")
            if as_json:
                emit_ndjson(
                    {"dry_run": True, "plan": loaded.id, "operations": len(loaded.operations)}
                )
            return
        if not yes and not typer.confirm(f"apply plan {loaded.id}?"):
            note("aborted")
            raise typer.Exit(2)
        outcome = apply_mod.run_apply(
            loaded, _cli().make_client(), backups_dir, log_dir, now=datetime.now(UTC), big=big
        )
        if as_json:
            emit_ndjson(
                {
                    "applied": outcome.applied,
                    "unchanged": outcome.unchanged,
                    "failed": outcome.failed,
                    "failures": outcome.failures,
                    "verified": outcome.verified,
                    "mismatches": outcome.mismatches,
                    "log": outcome.log_path,
                }
            )
        else:
            print(
                f"applied {outcome.applied}, unchanged {outcome.unchanged}, "
                f"failed {outcome.failed}"
            )
            for failure in outcome.failures:
                print(f"  failed {failure['key']}: {failure['code']} {failure['message']}")
            print(
                "verified: library state matches the plan"
                if outcome.verified
                else "VERIFICATION MISMATCH — compare the log against the library"
            )
            for mismatch in outcome.mismatches:
                print(f"  mismatch: {mismatch}")
            print(f"log: {outcome.log_path}")
        if outcome.failed or outcome.verified is False:
            raise typer.Exit(1)


def undo(
    session: Annotated[str, typer.Argument(help="Session id — the plan id that was applied.")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Verify only, touch nothing.")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation gate.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Final outcome object.")] = False,
):
    """Replay a session's change log backwards.

    Only applied entries reverse; every object is verified against its logged
    post-apply state first — drift is a conflict, reported and left untouched.

    Examples:
        zel undo 20260719T120000Z-merge-ai-tags --dry-run
        zel undo 20260719T120000Z-merge-ai-tags --yes
    """
    with _cli().guard():
        log_dir = config.ensure_dir("log")
        if not dry_run and not yes and not typer.confirm(f"undo session {session}?"):
            note("aborted")
            raise typer.Exit(2)
        try:
            outcome = undo_mod.run_undo(session, _cli().make_client(), log_dir, dry_run=dry_run)
        except undo_mod.UndoRefused as exc:
            note(f"refused: {exc}")
            raise typer.Exit(1) from None
        if as_json:
            emit_ndjson(
                {
                    "dry_run": dry_run,
                    "undone": outcome.undone,
                    "conflicts": outcome.conflicts,
                    "failures": outcome.failures,
                }
            )
        else:
            verb = "would undo" if dry_run else "undone"
            print(f"{verb}: {outcome.undone} operation(s)")
            for conflict in outcome.conflicts:
                print(f"  conflict: {conflict}")
            for failure in outcome.failures:
                print(f"  failed {failure['key']}: {failure['code']} {failure['message']}")
        if outcome.conflicts or outcome.failures:
            raise typer.Exit(1)


def reconcile(
    session: Annotated[str, typer.Argument(help="Session id with unresolved pending entries.")],
    as_json: Annotated[bool, typer.Option("--json", help="Final outcome object.")] = False,
):
    """Resolve a crashed apply's pending entries deterministically.

    Fetches the touched objects and compares them against each entry's logged
    old and new state — landed writes become applied, lost ones failed.

    Examples:
        zel debug reconcile 20260719T120000Z-merge-ai-tags
    """
    with _cli().guard():
        try:
            counts = recover_mod.run_reconcile(
                session, _cli().make_client(), config.ensure_dir("log")
            )
        except recover_mod.RestoreError as exc:
            note(f"error: {exc}")
            raise typer.Exit(2) from None
        if as_json:
            emit_ndjson(counts)
        else:
            print(f"reconciled: {counts['applied']} applied, {counts['failed']} failed")


def restore(
    backup: Annotated[str, typer.Argument(help="Backup id (resolved in backups/) or path.")],
    keys: Annotated[list[str], typer.Argument(help="Object keys to restore from it.")],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Resolve and count, touch nothing.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation gate.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Final outcome object.")] = False,
):
    """Layer-1 last resort: push backed-up JSON of exactly the named objects back.

    Deliberately unpinned to the backup's versions — recovery overwrites
    whatever state the accident left. Logged like any session.

    Examples:
        zel debug restore 20260719T115900Z AAAA1111 BBBB2222 --dry-run
        zel debug restore 20260719T115900Z AAAA1111 BBBB2222 --yes
    """
    with _cli().guard():
        path = _resolve(backup, config.ensure_dir("backups"), ".jsonl")
        if not dry_run and not yes and not typer.confirm(
            f"restore {len(keys)} object(s) from {path.name}? this overwrites their current state"
        ):
            note("aborted")
            raise typer.Exit(2)
        try:
            outcome = recover_mod.run_restore(
                path,
                list(keys),
                _cli().make_client(),
                config.ensure_dir("log"),
                now=datetime.now(UTC),
                dry_run=dry_run,
            )
        except recover_mod.RestoreError as exc:
            note(f"error: {exc}")
            raise typer.Exit(2) from None
        if as_json:
            emit_ndjson(
                {
                    "dry_run": dry_run,
                    "applied": outcome.applied,
                    "unchanged": outcome.unchanged,
                    "failed": outcome.failed,
                    "failures": outcome.failures,
                    "log": outcome.log_path,
                }
            )
        elif dry_run:
            print(f"would restore {outcome.applied} object(s) from {path.name} — nothing written")
        else:
            print(
                f"restored {outcome.applied}, unchanged {outcome.unchanged}, "
                f"failed {outcome.failed} — log: {outcome.log_path}"
            )
        if outcome.failed:
            raise typer.Exit(1)
