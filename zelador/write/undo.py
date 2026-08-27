"""zel undo — replay a session's change log backwards.

Only `applied` entries reverse. Before touching an object, its current state
must equal the logged new state — anything else is an undo conflict, reported
and left untouched (Zotero's server keeps no history; guessing would destroy
evidence). Coalesced entries verify against the last new and restore the first
old, mirroring apply's one-write-per-object composition. Undoing a
create_collection trashes the collection by its precomputed key, never a purge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import batched
from pathlib import Path

from zelador.client import BATCH_SIZE, ZoteroClient, ZoteroError
from zelador.write.changelog import LogEntry, SessionLog, read_log
from zelador.write.library_state import (
    facet_field,
    facet_value,
    fetch_objects,
    live_value,
    setting_value,
    state_equal,
)


class UndoRefused(Exception):
    """The session cannot be undone as it stands — nothing was written."""


@dataclass
class UndoOutcome:
    undone: int = 0
    conflicts: list[str] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)


def run_undo(
    session: str,
    client: ZoteroClient,
    log_dir: Path,
    dry_run: bool = False,
) -> UndoOutcome:
    """Reverse a session; with dry_run the verification runs but nothing is written,
    and `undone` counts what would reverse."""
    path = log_dir / f"{session}.jsonl"
    if not path.exists():
        raise UndoRefused(f"no session log named {session} in {log_dir}")
    _, entries = read_log(path)
    if any(entry.status == "pending" for entry in entries.values()):
        raise UndoRefused(
            f"session {session} has unresolved pending entries — "
            "run `zel debug reconcile` first"
        )
    applied = [entry for entry in entries.values() if entry.status == "applied"]
    outcome = UndoOutcome()
    if not applied:
        return outcome
    log = SessionLog(path)
    _undo_settings(client, log, applied, outcome, dry_run)
    _undo_objects(client, log, applied, outcome, dry_run)
    return outcome


def _undo_settings(client, log, applied: list[LogEntry], outcome: UndoOutcome, dry_run) -> None:
    for entry in applied:
        if entry.operation["kind"] != "setting":
            continue
        name = entry.operation["key"]
        if setting_value(client.setting(name)) != entry.operation["new"]:
            outcome.conflicts.append(f"{name}: setting changed since the apply — left untouched")
            continue
        if dry_run:
            outcome.undone += 1
            continue
        pin = client.last_modified_version or entry.version
        try:
            version = client.write_setting(name, entry.operation["old"], if_unmodified_since=pin)
        except ZoteroError as exc:
            outcome.failures.append({"key": name, "code": None, "message": str(exc)})
        else:
            log.resolve(entry.operation["id"], "undone", version)
            outcome.undone += 1


def _undo_objects(client, log, applied: list[LogEntry], outcome: UndoOutcome, dry_run) -> None:
    groups: dict[tuple[str, str], list[LogEntry]] = {}
    for entry in applied:
        if entry.operation["kind"] == "setting":
            continue
        op = entry.operation
        groups.setdefault((op["kind"], op["key"]), []).append(entry)
    if not groups:
        return
    current = fetch_objects(
        client,
        [key for kind, key in groups if kind == "item"],
        [key for kind, key in groups if kind == "collection"],
    )
    writes: list[tuple[str, dict, list[LogEntry]]] = []
    for (kind, key), group in groups.items():
        obj = current.get((kind, key))
        if obj is None:
            outcome.conflicts.append(f"{key}: no longer found — left untouched")
            continue
        undo_fields = _reverse_group(key, group, obj["data"], outcome)
        if undo_fields is None:
            continue
        writes.append((kind, {"key": key, "version": obj["version"], **undo_fields}, group))
    if dry_run:
        outcome.undone += sum(len(group) for _, _, group in writes)
        return
    # reverse of apply's order: item writes first, collection objects after
    for kind, write_fn in (("item", client.write_items), ("collection", client.write_collections)):
        batch = [w for w in writes if w[0] == kind]
        for chunk in batched(batch, BATCH_SIZE):
            _execute_chunk(write_fn, log, list(chunk), outcome)


def _reverse_group(key, group: list[LogEntry], data: dict, outcome: UndoOutcome) -> dict | None:
    """Verify each facet's current value against the last logged new state and
    compose the restore write from the first logged old. None means conflict."""
    by_facet: dict[str, list[LogEntry]] = {}
    for entry in group:
        by_facet.setdefault(entry.operation["facet"], []).append(entry)
    if "object" in by_facet:
        return _reverse_created(key, group, by_facet, data, outcome)
    undo_fields: dict = {}
    for facet, entries in by_facet.items():
        if not state_equal(facet, facet_value(data, facet), entries[-1].operation["new"]):
            outcome.conflicts.append(f"{key}: {facet} changed since the apply — left untouched")
            return None
        undo_fields[facet_field(facet)] = entries[0].operation["old"]
    return undo_fields


def _reverse_created(key, group, by_facet, data: dict, outcome: UndoOutcome) -> dict | None:
    """Undoing a create (collection or item) is a trash by the precomputed key,
    never a hard delete.

    The coalesced write carried the composed state — creation values overlaid
    by any same-plan facet ops — so verification composes the same way.
    """
    expected = dict(by_facet["object"][-1].operation["new"])
    for entry in group:
        facet = entry.operation["facet"]
        if facet != "object":
            expected[facet_field(facet)] = entry.operation["new"]
    matches = not data.get("deleted")
    for name, value in expected.items():
        live = live_value(data, name)
        if live is None and value == "":
            continue  # the server drops fields written as the empty string
        if not state_equal(name, live, value):
            matches = False
    if not matches:
        outcome.conflicts.append(
            f"{key}: object no longer matches the state this plan left — left untouched"
        )
        return None
    return {"deleted": True}


def _execute_chunk(write_fn, log, chunk, outcome: UndoOutcome) -> None:
    result = write_fn([write for _, write, _ in chunk])
    for _, write, group in chunk:
        key = write["key"]
        if key in result.applied or key in result.unchanged:
            version = result.applied.get(key, result.unchanged.get(key))
            for entry in group:
                log.resolve(entry.operation["id"], "undone", version)
            outcome.undone += len(group)
        else:
            error = result.failed.get(key) or {"code": None, "message": "no per-object result"}
            outcome.failures.append({"key": key, **error})
