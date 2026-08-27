"""The log.v1 write-ahead change log — layer 2 of the safety model.

Append-only JSONL: a header line, then entry lines. Every operation gets a
`pending` entry (carrying its full operation record, old state included)
before the write request goes out, and a resolution line after — so a crash
mid-apply never loses the undo record. The reader folds last-status-wins.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

LOG_SCHEMA = "log.v1"


class LogFormatError(Exception):
    """The file is not a session log — refused at the door rather than half-parsed."""


@dataclass
class LogEntry:
    operation: dict  # the plan operation record, as written with the pending line
    status: str  # pending | applied | unchanged | failed | undone
    version: int | None  # resulting object version, set by resolutions


class SessionLog:
    """Appending writer for one apply session; every line is flushed on write."""

    def __init__(self, path: Path):
        self.path = path

    def start(self, plan: str, backup: str, timestamp: str) -> None:
        self._append(
            {
                "kind": "header",
                "schema": LOG_SCHEMA,
                "plan": plan,
                "backup": backup,
                "timestamp": timestamp,
            }
        )

    def pending(self, operations: list[dict]) -> None:
        for operation in operations:
            self._append(
                {
                    "kind": "entry",
                    "op": operation["id"],
                    "status": "pending",
                    "operation": operation,
                }
            )

    def resolve(self, op_id: str, status: str, version: int | None = None) -> None:
        line: dict = {"kind": "entry", "op": op_id, "status": status}
        if version is not None:
            line["version"] = version
        self._append(line)

    def _append(self, line: dict) -> None:
        with self.path.open("a") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def is_session_log(path: Path) -> bool:
    """Whether this file declares itself a log.v1 session log in its first line."""
    try:
        read_header(path)
    except LogFormatError:
        return False
    return True


def read_header(path: Path) -> dict:
    """The header line, or a refusal naming the file.

    Other directories in the data dir hold one file per contract, but `log/` is
    shared — anything writing an audit trail lands beside the session logs. The
    header declares the schema, so check it before folding a single entry rather
    than discovering the mismatch as a KeyError on some later line.
    """
    with path.open() as handle:
        for raw in handle:
            if not raw.strip():
                continue
            try:
                line = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise LogFormatError(f"{path.name}: first line is not JSON — {exc}") from None
            return validate_header(line, path.name)
    raise LogFormatError(f"{path.name}: empty file, no header")


def validate_header(line, name: str) -> dict:
    """The one place the log.v1 header rule lives; both readers go through it."""
    if not isinstance(line, dict) or line.get("kind") != "header":
        raise LogFormatError(f"{name}: first line is not a header")
    schema = line.get("schema")
    if schema != LOG_SCHEMA:
        raise LogFormatError(f"{name}: not a {LOG_SCHEMA} session log (schema is {schema!r})")
    return line


def read_log(path: Path) -> tuple[dict, dict[str, LogEntry]]:
    """Header plus entries folded last-status-wins, in first-pending order.

    One pass: the first line is validated as the header, later header lines are
    skipped rather than adopted, so the header returned is always the one that
    passed the check.
    """
    header: dict | None = None
    entries: dict[str, LogEntry] = {}
    with path.open() as handle:
        for raw in handle:
            if not raw.strip():
                continue
            line = json.loads(raw)
            if header is None:
                header = validate_header(line, path.name)
                continue
            if line["kind"] == "header":
                continue
            if line["op"] in entries:
                entry = entries[line["op"]]
                entry.status = line["status"]
                entry.version = line.get("version", entry.version)
            else:
                entries[line["op"]] = LogEntry(
                    operation=line["operation"], status=line["status"], version=line.get("version")
                )
    if header is None:
        raise LogFormatError(f"{path.name}: empty file, no header")
    return header, entries


def unresolved_ops(path: Path) -> list[str]:
    """Operation ids whose last status is still `pending` — a crashed apply's residue."""
    _, entries = read_log(path)
    return [op_id for op_id, entry in entries.items() if entry.status == "pending"]
