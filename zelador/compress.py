"""The deterministic core of `zel compress`: what to try, what to accept, what may be moved.

Nothing here touches the filesystem or spawns Ghostscript — the shell in
`zelador.compress_files` does that and hands the facts back as data. The split
matters more than usual here: this is the only command whose writes land on file
bytes rather than the Web API, so `zel undo` can never reverse it, and the rules
that decide whether a compressed file is safe to install have to be testable
without producing one.

Every move is gated on content, never on a file merely being present. A scan
records the md5 of both the original and the compressed candidate; swap and
restore each refuse unless the bytes they are about to move and the bytes they
are about to displace are the ones those hashes describe. A half-copied file, a
file another run already replaced, and a file edited since the scan are all the
same class of problem, and one rule catches all three.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from zelador.output import render_table

CONTRACT = "compress.v1"

EntryId = tuple[str, str]


class CompressError(Exception):
    """Malformed run report or an impossible request — always fails loudly."""


@dataclass(frozen=True)
class Facts:
    """What a PDF is, reduced to the four things a verdict depends on."""

    bytes: int
    pages: int
    text_len: int
    annots: int = 0


@dataclass(frozen=True)
class Candidate:
    key: str
    filename: str
    bytes: int

    @property
    def id(self) -> EntryId:
        return (self.key, self.filename)


@dataclass(frozen=True)
class Entry:
    key: str
    filename: str
    accepted: bool
    reason: str
    before: Facts
    after: Facts
    original_md5: str  # the storage file as the scan found it
    staged_md5: str = ""  # the compressed candidate; empty when rejected

    @property
    def id(self) -> EntryId:
        """A storage folder may hold more than one file, so the key alone is not an identity."""
        return (self.key, self.filename)

    @property
    def label(self) -> str:
        return f"{self.key}/{self.filename}"


@dataclass(frozen=True)
class Report:
    run: str
    created: str
    zotero_dir: str
    settings: dict
    entries: list[Entry]


def allocate_run_id(now: datetime, existing: set[str]) -> str:
    """A run id nobody is using yet.

    Plan ids get uniqueness from their changeset slug; every compress run has the
    same slug, so two scans in the same second would otherwise share a directory
    and the second report would orphan the first run's originals.
    """
    base = f"{now.strftime('%Y%m%dT%H%M%SZ')}-compress"
    if base not in existing:
        return base
    for suffix in range(2, 1000):
        candidate = f"{base}-{suffix}"
        if candidate not in existing:
            return candidate
    raise CompressError(f"cannot allocate a run id: {base} and 998 suffixes are all taken")


def select_candidates(files: list[Candidate], min_bytes: int, limit: int | None) -> list[Candidate]:
    """PDFs above the floor, heaviest first — compression only pays on the big ones."""
    pdfs = [c for c in files if c.filename.lower().endswith(".pdf") and c.bytes >= min_bytes]
    pdfs.sort(key=lambda c: (-c.bytes, c.key, c.filename))
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
    shrank reports the lost page — that is the finding worth reading. Annotations
    rank with pages: a library's embedded highlights live in the file itself, and
    a compressor that silently flattened them would look like a clean win here.
    """
    if before.bytes <= 0:
        return False, "empty file"
    if before.pages != after.pages:
        return False, f"pages changed: {before.pages} -> {after.pages}"
    if after.annots < before.annots:
        return False, f"annotations lost: {before.annots} -> {after.annots}"
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
    entries: list[Entry], storage_md5s: dict[EntryId, str], staged_md5s: dict[EntryId, str]
) -> list[str]:
    """Reasons these entries must not be installed. Empty means every one is safe.

    Both sides are checked by content: the original because the staged copy was
    built from it, and the staged copy because a move that died halfway leaves a
    file that exists and is wrong.
    """
    blockers = []
    for entry in entries:
        storage = storage_md5s.get(entry.id)
        if storage is None:
            blockers.append(f"{entry.label}: no such file in storage any more")
        elif storage != entry.original_md5:
            blockers.append(f"{entry.label}: storage file changed since the scan — re-scan first")
        staged = staged_md5s.get(entry.id)
        if staged is None:
            blockers.append(f"{entry.label}: staged file is missing from the run directory")
        elif staged != entry.staged_md5:
            blockers.append(f"{entry.label}: staged file is damaged or incomplete — re-scan first")
    return blockers


def restore_blockers(
    entries: list[Entry], storage_md5s: dict[EntryId, str], quarantine_md5s: dict[EntryId, str]
) -> list[str]:
    """Reasons these originals must not be moved back.

    A missing storage file is *not* a blocker: that is what a swap interrupted
    between its two moves leaves behind, and putting the original back is exactly
    the repair. A storage file holding something other than what this run
    installed is a blocker — another run has been through here since.
    """
    blockers = []
    for entry in entries:
        quarantined = quarantine_md5s.get(entry.id)
        if quarantined is None:
            blockers.append(f"{entry.label}: no quarantined original to restore")
        elif quarantined != entry.original_md5:
            blockers.append(
                f"{entry.label}: quarantined original is damaged or incomplete — "
                "restoring it would overwrite the library with a partial file"
            )
        storage = storage_md5s.get(entry.id)
        if storage is not None and storage != entry.staged_md5:
            blockers.append(
                f"{entry.label}: the file in storage is not the one this run installed — "
                "another run has swapped it since"
            )
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


def with_entries(report: Report, entries: list[Entry]) -> Report:
    return replace(report, entries=entries)


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
                "original_md5": e.original_md5,
                "staged_md5": e.staged_md5,
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
                    original_md5=e["original_md5"],
                    staged_md5=e.get("staged_md5", ""),
                    before=_facts_from_dict(e["before"]),
                    after=_facts_from_dict(e["after"]),
                )
                for e in raw["entries"]
            ],
        )
    except (KeyError, TypeError) as exc:
        raise CompressError(f"malformed {CONTRACT} report: {exc}") from None


def _facts_to_dict(f: Facts) -> dict:
    return {"bytes": f.bytes, "pages": f.pages, "text_len": f.text_len, "annots": f.annots}


def _facts_from_dict(raw: dict) -> Facts:
    return Facts(
        bytes=raw["bytes"],
        pages=raw["pages"],
        text_len=raw["text_len"],
        annots=raw.get("annots", 0),
    )
