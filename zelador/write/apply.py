"""zel apply — execute an expanded plan through the write-ahead change log.

Ops on one object were coalesced at expansion; here they become exactly one
write. Pending log entries land before every request, so a crash never loses
the undo record — a transport failure mid-apply propagates and leaves them
pending for `zel debug reconcile`. After the writes, the touched objects are
fetched back and compared against the plan: success is verified, not assumed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from itertools import batched
from pathlib import Path

from zelador.client import BATCH_SIZE, ZoteroClient, ZoteroError
from zelador.status import pending_sessions
from zelador.write.changelog import SessionLog
from zelador.write.contracts import Operation, Plan
from zelador.write.library_state import facet_field, fetch_objects, live_value, state_equal

BIG_THRESHOLD = 200  # objects; beyond this apply refuses without --big


class ApplyRefused(Exception):
    """A precondition failed — nothing was written."""


@dataclass
class ApplyOutcome:
    applied: int = 0
    unchanged: int = 0
    failed: int = 0
    failures: list[dict] = field(default_factory=list)  # {key, code, message}
    verified: bool | None = None
    mismatches: list[str] = field(default_factory=list)
    log_path: str = ""


def check_preconditions(plan: Plan, backups_dir: Path, log_dir: Path, big: bool) -> None:
    if not (backups_dir / f"{plan.backup}.jsonl").exists():
        raise ApplyRefused(
            f"plan is pinned to backup {plan.backup}, which is missing from {backups_dir} — "
            "that exact backup is the restore path this plan was validated against"
        )
    unresolved = pending_sessions(log_dir)
    if unresolved:
        raise ApplyRefused(
            f"session(s) with unresolved pending entries: {', '.join(unresolved)} — "
            "run `zel debug reconcile <session>` first"
        )
    if (log_dir / f"{plan.id}.jsonl").exists():
        raise ApplyRefused(
            f"plan {plan.id} already has a session log — plans apply once; "
            "re-validate into a fresh plan instead"
        )
    count = len({(op.kind, op.key) for op in plan.operations}) + (1 if plan.settings else 0)
    if count > BIG_THRESHOLD and not big:
        raise ApplyRefused(f"plan touches {count} objects (>{BIG_THRESHOLD}) — re-run with --big")


def compose_writes(operations: list[Operation]) -> list[tuple[str, dict, list[Operation]]]:
    """One write object per (kind, key): the composed final state of every touched facet."""
    groups: dict[tuple[str, str], list[Operation]] = {}
    for op in operations:
        groups.setdefault((op.kind, op.key), []).append(op)
    writes = []
    for (kind, key), ops in groups.items():
        write = {"key": key, "version": ops[0].version}
        for op in ops:
            if op.facet == "object":
                write.update(op.new)
            else:
                write[facet_field(op.facet)] = op.new
        writes.append((kind, write, ops))
    return writes


def run_apply(
    plan: Plan,
    client: ZoteroClient,
    backups_dir: Path,
    log_dir: Path,
    now: datetime,
    big: bool = False,
) -> ApplyOutcome:
    check_preconditions(plan, backups_dir, log_dir, big)
    log = SessionLog(log_dir / f"{plan.id}.jsonl")
    log.start(plan=plan.id, backup=plan.backup, timestamp=now.strftime("%Y%m%dT%H%M%SZ"))
    outcome = ApplyOutcome(log_path=str(log.path))
    writes = compose_writes(plan.operations)
    applied_writes: list[tuple[str, dict]] = []
    # collections first: item memberships may reference collections created in this plan
    for kind in ("collection", "item"):
        write_fn = client.write_collections if kind == "collection" else client.write_items
        batch = [
            (write, [asdict(op) for op in ops]) for k, write, ops in writes if k == kind
        ]
        for chunk in batched(batch, BATCH_SIZE):
            applied = execute_chunk(write_fn, log, list(chunk), outcome)
            applied_writes.extend((kind, write) for write in applied)
    if plan.settings:
        _write_settings(client, log, plan.settings, outcome)
    _verify(client, applied_writes, outcome)
    return outcome


def execute_chunk(
    write_fn, log: SessionLog, chunk: list[tuple[dict, list[dict]]], outcome: ApplyOutcome
) -> list[dict]:
    """Write-ahead one chunk: pending entries, one request, per-object resolutions.

    `chunk` pairs each write object with its operation records; returns the
    writes that applied (for verification). Shared with zel debug restore.
    """
    log.pending([op for _, ops in chunk for op in ops])
    result = write_fn([write for write, _ in chunk])
    applied_writes = []
    for write, ops in chunk:
        key = write["key"]
        if key in result.applied:
            for op in ops:
                log.resolve(op["id"], "applied", result.applied[key])
            outcome.applied += len(ops)
            applied_writes.append(write)
        elif key in result.unchanged:
            for op in ops:
                log.resolve(op["id"], "unchanged", result.unchanged[key])
            outcome.unchanged += len(ops)
        else:
            error = result.failed.get(key) or {"code": None, "message": "no per-object result"}
            for op in ops:
                log.resolve(op["id"], "failed")
            outcome.failed += len(ops)
            outcome.failures.append({"key": key, **error})
    return applied_writes


def _write_settings(client, log, settings: dict, outcome: ApplyOutcome) -> None:
    log.pending(
        [
            {
                "id": "settings",
                "kind": "setting",
                "key": settings["name"],
                "facet": "setting",
                "version": settings["version"],
                "old": settings["old"],
                "new": settings["new"],
            }
        ]
    )
    # Conditional writes check the setting's own version, so the plan's
    # library-version pin still guards this write after our own item writes:
    # only a post-validation change to the setting can move it past the pin.
    try:
        version = client.write_setting(
            settings["name"], settings["new"], if_unmodified_since=settings["version"]
        )
    except ZoteroError as exc:
        log.resolve("settings", "failed")
        outcome.failed += 1
        outcome.failures.append({"key": settings["name"], "code": None, "message": str(exc)})
    else:
        log.resolve("settings", "applied", version)
        outcome.applied += 1


def _verify(client, applied_writes: list[tuple[str, dict]], outcome: ApplyOutcome) -> None:
    """Fetch exactly the touched objects back and compare against the composed writes."""
    current = fetch_objects(
        client,
        [w["key"] for kind, w in applied_writes if kind == "item"],
        [w["key"] for kind, w in applied_writes if kind == "collection"],
    )
    for kind, write in applied_writes:
        obj = current.get((kind, write["key"]))
        if obj is None:
            outcome.mismatches.append(f"{write['key']}: not found on re-read")
            continue
        data = obj["data"]
        for name, value in write.items():
            if name in ("key", "version"):
                continue
            live = live_value(data, name)
            if live is None and value == "":
                continue  # the server drops fields written as the empty string
            if not state_equal(name, live, value):
                outcome.mismatches.append(f"{write['key']}: {name} does not match the plan")
    outcome.verified = not outcome.mismatches


def summarize(plan: Plan) -> list[str]:
    """The dry-run rendering: per intent group, what would be written."""
    lines = [
        f"plan {plan.id} — validated at library version {plan.library_version}, "
        f"backup {plan.backup}"
    ]
    for group, intent in enumerate(plan.intents):
        ops = [op for op in plan.operations if op.group == group]
        high = sum(1 for op in ops if op.risk == "high")
        detail = ", ".join(f"{k}={v}" for k, v in intent.items() if k != "op")
        risk_note = f", {high} high-risk" if high else ""
        lines.append(
            f"  group {group} {intent['op']}({detail}): {len(ops)} operation(s){risk_note}"
        )
    if plan.settings:
        lines.append(f"  settings: tagColors — {len(plan.settings['new'])} colour assignment(s)")
    objects = len({(op.kind, op.key) for op in plan.operations})
    writes = objects + (1 if plan.settings else 0)
    lines.append(
        f"total: {len(plan.operations)} operation(s) on {objects} object(s), "
        f"{writes} write request unit(s)"
    )
    return lines
