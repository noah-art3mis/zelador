"""Recovery utilities: zel debug reconcile and zel debug restore.

Reconcile resolves a crashed apply's pending entries deterministically: fetch
the touched objects and compare against the entry's logged states — current
state equal to the logged new means the write landed (applied); anything else,
the still-old state included, ends the operation's story as failed. Restore is
layer 1's named path — push the backed-up JSON of exactly the named objects
back through the standard write machinery, deliberately unpinned to the
backup's versions because last-resort recovery overwrites whatever state the
accident left.
"""

from __future__ import annotations

import json
from datetime import datetime
from itertools import batched
from pathlib import Path

from zelador.client import BATCH_SIZE, ZoteroClient
from zelador.write.apply import ApplyOutcome, execute_chunk
from zelador.write.changelog import SessionLog, read_log
from zelador.write.library_state import facet_value, fetch_objects, setting_value, state_equal


class RestoreError(Exception):
    """Recovery could not start — nothing was written."""


def run_reconcile(session: str, client: ZoteroClient, log_dir: Path) -> dict:
    path = log_dir / f"{session}.jsonl"
    if not path.exists():
        raise RestoreError(f"no session log named {session} in {log_dir}")
    _, entries = read_log(path)
    pending = [entry for entry in entries.values() if entry.status == "pending"]
    log = SessionLog(path)
    counts = {"applied": 0, "failed": 0}
    current = fetch_objects(
        client,
        [e.operation["key"] for e in pending if e.operation["kind"] == "item"],
        [e.operation["key"] for e in pending if e.operation["kind"] == "collection"],
    )
    for entry in pending:
        op = entry.operation
        landed, version = _landed(client, current, op)
        if landed:
            log.resolve(op["id"], "applied", version)
            counts["applied"] += 1
        else:
            log.resolve(op["id"], "failed")
            counts["failed"] += 1
    return counts


def _landed(client, current, op) -> tuple[bool, int | None]:
    if op["kind"] == "setting":
        live = client.setting(op["key"])
        return (setting_value(live) == op["new"], live["version"] if live else None)
    obj = current.get((op["kind"], op["key"]))
    if obj is None:
        return False, None
    if op["facet"] == "object":
        data, created = obj["data"], op["new"]
        landed = (
            data.get("name") == created["name"]
            and data.get("parentCollection", False) == created["parentCollection"]
        )
        return landed, obj["version"]
    facet = op["facet"]
    return state_equal(facet, facet_value(obj["data"], facet), op["new"]), obj["version"]


def run_restore(
    backup_path: Path,
    keys: list[str],
    client: ZoteroClient,
    log_dir: Path,
    now: datetime,
    dry_run: bool = False,
) -> ApplyOutcome:
    """Push the named objects' backup JSON back; with dry_run nothing is written,
    no session log is opened, and `applied` counts what would restore."""
    objects = _read_backup(backup_path)
    missing = [key for key in keys if key not in objects]
    if missing:
        raise RestoreError(f"key(s) not in backup {backup_path.name}: {', '.join(missing)}")
    current = fetch_objects(
        client,
        [key for key in keys if objects[key][0] == "item"],
        [key for key in keys if objects[key][0] == "collection"],
    )
    if dry_run:
        return ApplyOutcome(applied=len(keys))
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    session = f"{stamp}-restore"
    log = SessionLog(log_dir / f"{session}.jsonl")
    log.start(plan=session, backup=backup_path.stem, timestamp=stamp)
    outcome = ApplyOutcome(log_path=str(log.path))
    counter = iter(range(1, len(keys) + 1))
    for kind, write_fn in (("collection", client.write_collections), ("item", client.write_items)):
        batch = []
        for key in (k for k in keys if objects[k][0] == kind):
            data = dict(objects[key][1])
            live = current.get((kind, key))
            data["key"] = key
            data["version"] = live["version"] if live else 0  # current, never the backup's pin
            operation = {
                "id": f"op-{next(counter):03d}",
                "kind": kind,
                "key": key,
                "version": data["version"],
                "facet": "object",
                "old": live["data"] if live else None,
                "new": objects[key][1],
                "risk": "high",
            }
            batch.append((data, [operation]))
        for chunk in batched(batch, BATCH_SIZE):
            execute_chunk(write_fn, log, list(chunk), outcome)
    return outcome


def _read_backup(backup_path: Path) -> dict[str, tuple[str, dict]]:
    """key -> (kind, data) for every item and collection in the backup file."""
    try:
        handle = backup_path.open()
    except FileNotFoundError:
        raise RestoreError(f"no such backup: {backup_path}") from None
    objects: dict[str, tuple[str, dict]] = {}
    with handle:
        for raw in handle:
            line = json.loads(raw)
            if line["kind"] in ("item", "collection"):
                obj = line["object"]
                objects[obj["key"]] = (line["kind"], obj["data"])
    return objects
