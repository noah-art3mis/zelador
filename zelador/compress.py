"""The deterministic core of `zel compress`: what to try, what to accept, what may be swapped.

Nothing here touches the filesystem or spawns Ghostscript — the shell in
`zelador.compress_files` does that and hands the facts back as data. The split
matters more than usual here: this is the only command whose writes land on file
bytes rather than the Web API, so `zel undo` can never reverse it, and the rules
that decide whether a compressed file is safe to install have to be testable
without producing one.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from zelador.output import render_table

CONTRACT = "compress.v1"


class CompressError(Exception):
    """Malformed run report or an impossible request — always fails loudly."""


@dataclass(frozen=True)
class Facts:
    """What a PDF is, reduced to the three things a verdict depends on."""

    bytes: int
    pages: int
    text_len: int


@dataclass(frozen=True)
class Candidate:
    key: str
    filename: str
    bytes: int


@dataclass(frozen=True)
class Entry:
    key: str
    filename: str
    accepted: bool
    reason: str
    before: Facts
    after: Facts
    md5: str  # of the storage file as scan found it — the swap-time precondition


@dataclass(frozen=True)
class Report:
    run: str
    created: str
    zotero_dir: str
    settings: dict
    entries: list[Entry]


def run_id(now: datetime) -> str:
    """Run ids sort chronologically and read as plan ids do: <UTC stamp>-<slug>."""
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-compress"


def select_candidates(
    files: list[Candidate], min_bytes: int, limit: int | None
) -> list[Candidate]:
    """PDFs above the floor, heaviest first — compression only pays on the big ones."""
    pdfs = [c for c in files if c.filename.lower().endswith(".pdf") and c.bytes >= min_bytes]
    pdfs.sort(key=lambda c: (-c.bytes, c.key))
    return pdfs[:limit] if limit is not None else pdfs


def saving(before_bytes: int, after_bytes: int) -> float:
    """Fraction of the original reclaimed; negative when the file grew."""
    if before_bytes <= 0:
        raise CompressError("cannot compute a saving against an empty original")
    return 1 - after_bytes / before_bytes


def verdict(
    before: Facts, after: Facts, *, min_saving: float, text_tolerance: float
) -> tuple[bool, str]:
    """Accept the compressed file, or say which rule it broke.

    Checks run most-alarming first, so a file that both lost a page and barely
    shrank reports the lost page — that is the finding worth reading.
    """
    if before.bytes <= 0:
        return False, "empty file"
    if before.pages != after.pages:
        return False, f"pages changed: {before.pages} -> {after.pages}"
    floor = before.text_len * (1 - text_tolerance)
    if before.text_len > 0 and after.text_len < floor:
        return False, f"text layer shrank: {before.text_len} -> {after.text_len} chars"
    cut = saving(before.bytes, after.bytes)
    if cut < min_saving:
        return False, f"saved {cut * 100:.1f}%, below the {min_saving * 100:.0f}% floor"
    return True, f"saved {cut * 100:.1f}%"


def accepted(report: Report) -> list[Entry]:
    return [e for e in report.entries if e.accepted]


def swap_blockers(
    report: Report, current_md5s: dict[str, str], staged_present: dict[str, bool]
) -> list[str]:
    """Reasons this run must not be installed. Empty means every accepted entry is safe.

    Only accepted entries are ever swapped, so drift under a rejected candidate
    is none of this command's business.
    """
    blockers = []
    for e in accepted(report):
        current = current_md5s.get(e.key)
        if current is None:
            blockers.append(f"{e.key}: no file at {e.filename} in storage any more")
        elif current != e.md5:
            blockers.append(f"{e.key}: storage file changed since the scan — re-scan first")
        if not staged_present.get(e.key):
            blockers.append(f"{e.key}: staged file is missing from the run directory")
    return blockers


def summarize(report: Report) -> list[str]:
    """Table of every candidate with its verdict, accepted first."""
    order = sorted(report.entries, key=lambda e: (not e.accepted, -e.before.bytes))
    rows = [
        (
            e.key,
            "keep" if e.accepted else "skip",
            _mb(e.before.bytes),
            _mb(e.after.bytes),
            e.reason,
            e.filename[:44],
        )
        for e in order
    ]
    lines = render_table(["item", "verdict", "before", "after", "why", "file"], rows)
    kept = accepted(report)
    before = sum(e.before.bytes for e in kept)
    after = sum(e.after.bytes for e in kept)
    cut = f"{saving(before, after) * 100:.1f}%" if before else "0.0%"
    lines.append("")
    lines.append(
        f"{len(kept)} of {len(report.entries)} accepted: {_mb(before)} -> {_mb(after)} ({cut})"
    )
    return lines


def _mb(n: int) -> str:
    return f"{n / 1048576:.1f} MB"


def report_to_dict(report: Report) -> dict:
    return {
        "contract": CONTRACT,
        "run": report.run,
        "created": report.created,
        "zotero_dir": report.zotero_dir,
        "settings": report.settings,
        "entries": [
            {
                "key": e.key,
                "filename": e.filename,
                "accepted": e.accepted,
                "reason": e.reason,
                "md5": e.md5,
                "before": _facts_to_dict(e.before),
                "after": _facts_to_dict(e.after),
            }
            for e in report.entries
        ],
    }


def report_from_dict(raw: dict) -> Report:
    contract = raw.get("contract")
    if contract != CONTRACT:
        raise CompressError(f"unsupported report contract: {contract!r}, expected {CONTRACT!r}")
    try:
        return Report(
            run=raw["run"],
            created=raw["created"],
            zotero_dir=raw["zotero_dir"],
            settings=raw.get("settings", {}),
            entries=[
                Entry(
                    key=e["key"],
                    filename=e["filename"],
                    accepted=e["accepted"],
                    reason=e["reason"],
                    md5=e["md5"],
                    before=_facts_from_dict(e["before"]),
                    after=_facts_from_dict(e["after"]),
                )
                for e in raw["entries"]
            ],
        )
    except (KeyError, TypeError) as exc:
        raise CompressError(f"malformed {CONTRACT} report: {exc}") from None


def with_entries(report: Report, entries: list[Entry]) -> Report:
    return replace(report, entries=entries)


def _facts_to_dict(f: Facts) -> dict:
    return {"bytes": f.bytes, "pages": f.pages, "text_len": f.text_len}


def _facts_from_dict(raw: dict) -> Facts:
    return Facts(bytes=raw["bytes"], pages=raw["pages"], text_len=raw["text_len"])
