"""Imperative shell for `zel compress`: Ghostscript, hashes, and the three file moves.

The run directory *is* the state. A file present in `originals/` means that key has
been swapped; nothing else records it, so there is no bookkeeping file to disagree
with the disk. Swap and restore are inverses that move the same two files between
`staged/`, `originals/`, and Zotero's storage — a crash between the two moves leaves
the original in quarantine, which is exactly what restore knows how to undo.
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
    Facts,
    Report,
    accepted,
    run_id,
    verdict,
)

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
    if preset not in PRESETS:
        raise CompressError(f"unknown preset {preset!r} — one of {', '.join(PRESETS)}")
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
    """Size, page count and extracted text length — everything a verdict needs."""
    from pypdf import PdfReader

    try:
        pages = [page.extract_text() or "" for page in PdfReader(path).pages]
    except Exception as exc:  # pypdf raises a zoo of parse errors
        raise CompressError(f"pypdf could not read {path.name}: {exc}") from None
    return Facts(
        bytes=path.stat().st_size, pages=len(pages), text_len=sum(len(page) for page in pages)
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

    Rejected output is deleted rather than left staged, so anything still under
    `staged/` when this returns is installable.
    """
    entries = []
    for candidate in candidates:
        source = storage_path(zotero_dir, candidate.key, candidate.filename)
        staged = staged_path(run_dir, candidate.key, candidate.filename)
        digest = md5_of(source)
        before = pdf_facts(source)
        try:
            compress_pdf(source, staged, preset)
            after = pdf_facts(staged)
        except CompressError as exc:
            _discard(staged)
            entries.append(
                Entry(
                    key=candidate.key,
                    filename=candidate.filename,
                    accepted=False,
                    reason=str(exc),
                    before=before,
                    after=before,
                    md5=digest,
                )
            )
            continue
        ok, reason = verdict(
            before, after, min_saving=min_saving, text_tolerance=text_tolerance
        )
        if not ok:
            _discard(staged)
        entry = Entry(
            key=candidate.key,
            filename=candidate.filename,
            accepted=ok,
            reason=reason,
            before=before,
            after=after,
            md5=digest,
        )
        entries.append(entry)
        if progress:
            progress(entry)
    return Report(
        run=run_id(now),
        created=now.isoformat(),
        zotero_dir=str(zotero_dir),
        settings={
            "preset": preset,
            "min_saving": min_saving,
            "text_tolerance": text_tolerance,
        },
        entries=entries,
    )


def swapped_entries(report: Report, run_dir: Path) -> list[Entry]:
    """Accepted entries already installed — a quarantined original is the only record."""
    return [e for e in accepted(report) if quarantine_path(run_dir, e.key, e.filename).exists()]


def pending_entries(report: Report, run_dir: Path) -> list[Entry]:
    """Accepted entries not yet installed — the complement of the swapped ones."""
    done = {e.key for e in swapped_entries(report, run_dir)}
    return [e for e in accepted(report) if e.key not in done]


def current_md5s(entries: list[Entry], zotero_dir: Path) -> dict[str, str]:
    live = {}
    for entry in entries:
        path = storage_path(zotero_dir, entry.key, entry.filename)
        if path.exists():
            live[entry.key] = md5_of(path)
    return live


def staged_present(entries: list[Entry], run_dir: Path) -> dict[str, bool]:
    return {e.key: staged_path(run_dir, e.key, e.filename).exists() for e in entries}


def perform_swap(entries: list[Entry], run_dir: Path, zotero_dir: Path) -> list[Entry]:
    """Quarantine each original, then install its staged replacement. Returns what moved."""
    moved = []
    for entry in entries:
        storage = storage_path(zotero_dir, entry.key, entry.filename)
        quarantine = quarantine_path(run_dir, entry.key, entry.filename)
        staged = staged_path(run_dir, entry.key, entry.filename)
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(storage), str(quarantine))
        shutil.move(str(staged), str(storage))
        moved.append(entry)
    return moved


def perform_restore(report: Report, run_dir: Path, zotero_dir: Path) -> list[Entry]:
    """Exact inverse of swap: the compressed file returns to staging, the original to storage."""
    restored = []
    for entry in swapped_entries(report, run_dir):
        quarantine = quarantine_path(run_dir, entry.key, entry.filename)
        storage = storage_path(zotero_dir, entry.key, entry.filename)
        staged = staged_path(run_dir, entry.key, entry.filename)
        if storage.exists():
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(storage), str(staged))
        storage.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(quarantine), str(storage))
        _prune(quarantine.parent)
        restored.append(entry)
    _prune(run_dir / "originals")
    return restored


def _discard(path: Path) -> None:
    path.unlink(missing_ok=True)
    _prune(path.parent)


def _prune(directory: Path) -> None:
    """Remove a directory once it has served its purpose; never touch a non-empty one."""
    if directory.is_dir() and not any(directory.iterdir()):
        directory.rmdir()
