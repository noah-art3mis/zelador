"""Shared pypdf reads: page text and annotation counts, behind one error type.

Two commands need the same facts out of a local PDF — `zel lookup fulltext` wants
the text, `zel compress` wants the text plus how many annotation objects the file
carries — and pypdf raises a zoo of parse errors that each caller would otherwise
have to know about.
"""

from __future__ import annotations

from pathlib import Path


class PdfReadError(Exception):
    """The file could not be parsed as a PDF."""


def scan_pages(path: Path) -> tuple[list[str], int]:
    """Extracted text per page, and the total number of annotation objects.

    Annotations are counted rather than inspected: a highlight embedded in the
    file is an object under the page's /Annots, and losing one is what a
    compressor that flattens markup into the page image looks like.
    """
    from pypdf import PdfReader

    try:
        texts, annots = [], 0
        for page in PdfReader(path).pages:
            texts.append(page.extract_text() or "")
            annots += _annotation_count(page)
        return texts, annots
    except Exception as exc:  # pypdf raises a zoo of parse errors
        raise PdfReadError(f"pypdf could not read {path.name}: {exc}") from None


def page_texts(path: Path) -> list[str]:
    return scan_pages(path)[0]


def _annotation_count(page) -> int:
    """/Annots may be the array itself or a reference to it; both are valid PDF."""
    annots = page.get("/Annots")
    if annots is None:
        return 0
    resolved = annots.get_object() if hasattr(annots, "get_object") else annots
    return len(resolved or [])
