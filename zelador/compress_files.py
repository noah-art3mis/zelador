"""Imperative shell for `zel compress`: Ghostscript, hashes, and the file moves.

The run directory holds the state: `staged/<key>/<file>` is a compressed candidate,
`originals/<key>/<file>` is the untouched original a swap moved out of the way. But
presence alone is never taken as proof — the report records the md5 of both, and
`zelador.compress` refuses any move whose bytes do not match. That matters because
the data dir and Zotero's storage are usually on different filesystems (ext4 and
DrvFs under WSL), so a "move" is a copy followed by an unlink: interrupt it and a
real file exists holding partial content.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from zelador.compress import (
    Candidate,
    CompressError,
    Entry,
    EntryId,
    Facts,
    Report,
    accepted,
    verdict,
)
from zelador.pdf import PdfReadError, scan_pages

PRESETS = ("screen", "ebook", "printer", "prepress")
_HASH_CHUNK = 1 << 20


def ensure_ghostscript() -> str:
    """Ghostscript is the whole engine — say so before doing any work."""
    gs = shutil.which("gs")
    if gs is None:
        raise CompressError(
            "ghostscript is not installed — `sudo apt install ghostscript` "
            "(macOS: `brew install ghostscript`)"
        )
    return gs


def compress_pdf(src: Path, dst: Path, preset: str) -> None:
    """Rewrite src through Ghostscript's pdfwrite device at the given quality preset."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            ensure_ghostscript(),
            "-sDEVICE=pdfwrite",
            "-dCompatibilityLevel=1.5",
            f"-dPDFSETTINGS=/{preset}",
            "-dNOPAUSE",
            "-dQUIET",
            "-dBATCH",
            "-dSAFER",
            f"-sOutputFile={dst}",
            str(src),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not dst.exists():
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise CompressError(f"ghostscript failed on {src.name}: {detail[-1] if detail else '?'}")


def md5_of(path: Path) -> str:
    digest = hashlib.md5()  # matches the digest Zotero itself records per attachment
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pdf_facts(path: Path) -> Facts:
    """Size, pages, extracted text length and annotation count — all a verdict needs."""
    try:
        texts, annots = scan_pages(path)
    except PdfReadError as exc:
        raise CompressError(str(exc)) from None
    return Facts(
        bytes=path.stat().st_size,
        pages=len(texts),
        text_len=sum(len(text) for text in texts),
        annots=annots,
    )


def storage_files(zotero_dir: Path, keys: list[str] | None = None) -> list[Candidate]:
    """Every payload file under storage/, optionally narrowed to given attachment keys."""
    storage = zotero_dir / "storage"
    if not storage.is_dir():
        raise CompressError(f"no storage directory under {zotero_dir}")
    wanted = set(keys) if keys else None
    found = []
    for folder in sorted(storage.iterdir()):
        if not folder.is_dir() or (wanted is not None and folder.name not in wanted):
            continue
        for path in sorted(folder.iterdir()):
            if path.is_file() and not path.name.startswith(".zotero-"):
                found.append(
                    Candidate(key=folder.name, filename=path.name, bytes=path.stat().st_size)
                )
    if wanted is not None:
        missing = wanted - {c.key for c in found}
        if missing:
            raise CompressError(f"no stored file for: {', '.join(sorted(missing))}")
    return found


def storage_path(zotero_dir: Path, key: str, filename: str) -> Path:
    return zotero_dir / "storage" / key / filename


def staged_path(run_dir: Path, key: str, filename: str) -> Path:
    return run_dir / "staged" / key / filename


def quarantine_path(run_dir: Path, key: str, filename: str) -> Path:
    return run_dir / "originals" / key / filename


def existing_runs(compress_dir: Path) -> set[str]:
    if not compress_dir.is_dir():
        return set()
    return {path.name for path in compress_dir.iterdir() if path.is_dir()}


def perform_scan(
    zotero_dir: Path,
    run_dir: Path,
    candidates: list[Candidate],
    *,
    preset: str,
    min_saving: float,
    text_tolerance: float,
    now: datetime,
    progress=None,
) -> Report:
    """Compress each candidate into the run's staging area and judge the result.

    A candidate that cannot be read or compressed is recorded as a rejection and
    the scan continues: one unreadable file must not discard the Ghostscript
    minutes already spent on every other candidate.
    """
    entries = []
    for candidate in candidates:
        entry = _scan_one(zotero_dir, run_dir, candidate, preset, min_saving, text_tolerance)
        entries.append(entry)
        if progress:
            progress(entry)
    return Report(
        run=run_dir.name,
        created=now.isoformat(),
        zotero_dir=str(zotero_dir),
        settings={"preset": preset, "min_saving": min_saving, "text_tolerance": text_tolerance},
        entries=entries,
    )


def _scan_one(
    zotero_dir: Path,
    run_dir: Path,
    candidate: Candidate,
    preset: str,
    min_saving: float,
    text_tolerance: float,
) -> Entry:
    source = storage_path(zotero_dir, candidate.key, candidate.filename)
    staged = staged_path(run_dir, candidate.key, candidate.filename)
    unknown = Facts(bytes=candidate.bytes, pages=0, text_len=0)
    digest = ""
    try:
        digest = md5_of(source)
        before = pdf_facts(source)
        compress_pdf(source, staged, preset)
        after = pdf_facts(staged)
    except (CompressError, OSError) as exc:
        _discard(staged)
        return Entry(
            key=candidate.key,
            filename=candidate.filename,
            accepted=False,
            reason=str(exc),
            before=unknown,
            after=unknown,
            original_md5=digest,
        )
    ok, reason = verdict(before, after, min_saving=min_saving, text_tolerance=text_tolerance)
    if not ok:
        _discard(staged)
    return Entry(
        key=candidate.key,
        filename=candidate.filename,
        accepted=ok,
        reason=reason,
        before=before,
        after=after,
        original_md5=digest,
        staged_md5=md5_of(staged) if ok else "",
    )


def swapped_entries(report: Report, run_dir: Path) -> list[Entry]:
    """Accepted entries whose original has been moved into quarantine.

    Whether that quarantined file is *intact* is a separate question, answered by
    `restore_blockers` — an entry with a damaged original must still be reported,
    not quietly skipped.
    """
    return [e for e in accepted(report) if quarantine_path(run_dir, e.key, e.filename).exists()]


def pending_entries(report: Report, run_dir: Path) -> list[Entry]:
    """Accepted entries not yet installed — the complement of the swapped ones."""
    done = {e.id for e in swapped_entries(report, run_dir)}
    return [e for e in accepted(report) if e.id not in done]


def _md5s(paths: dict[EntryId, Path]) -> dict[EntryId, str]:
    return {key: md5_of(path) for key, path in paths.items() if path.exists()}


def storage_md5s(entries: list[Entry], zotero_dir: Path) -> dict[EntryId, str]:
    return _md5s({e.id: storage_path(zotero_dir, e.key, e.filename) for e in entries})


def staged_md5s(entries: list[Entry], run_dir: Path) -> dict[EntryId, str]:
    return _md5s({e.id: staged_path(run_dir, e.key, e.filename) for e in entries})


def quarantine_md5s(entries: list[Entry], run_dir: Path) -> dict[EntryId, str]:
    return _md5s({e.id: quarantine_path(run_dir, e.key, e.filename) for e in entries})


def perform_swap(entries: list[Entry], run_dir: Path, zotero_dir: Path) -> list[Entry]:
    """Quarantine each original, then install its staged replacement. Returns what moved."""
    moved = []
    for entry in entries:
        storage = storage_path(zotero_dir, entry.key, entry.filename)
        quarantine = quarantine_path(run_dir, entry.key, entry.filename)
        _move(storage, quarantine)
        _move(staged_path(run_dir, entry.key, entry.filename), storage)
        moved.append(entry)
    return moved


def perform_restore(entries: list[Entry], run_dir: Path, zotero_dir: Path) -> list[Entry]:
    """Exact inverse of swap: the compressed file returns to staging, the original to storage."""
    restored = []
    for entry in entries:
        quarantine = quarantine_path(run_dir, entry.key, entry.filename)
        storage = storage_path(zotero_dir, entry.key, entry.filename)
        if storage.exists():
            _move(storage, staged_path(run_dir, entry.key, entry.filename))
        _move(quarantine, storage)
        _prune(quarantine.parent)
        restored.append(entry)
    _prune(run_dir / "originals")
    return restored


def _move(src: Path, dst: Path) -> None:
    """Move a file, reporting the failure in this command's own terms.

    `shutil.move` across filesystems copies then unlinks, so an ENOSPC here leaves
    a partial destination — which is why every mover is content-checked before the
    next command trusts what it finds.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(src), str(dst))
    except OSError as exc:
        raise CompressError(f"could not move {src.name} to {dst.parent}: {exc}") from None


def _discard(path: Path) -> None:
    path.unlink(missing_ok=True)
    _prune(path.parent)


def _prune(directory: Path) -> None:
    """Remove a directory once it has served its purpose; never touch a non-empty one."""
    if directory.is_dir() and not any(directory.iterdir()):
        directory.rmdir()
