"""Shared pypdf reads — the shapes real library PDFs actually take."""

from __future__ import annotations

import pytest
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject

from zelador.pdf import PdfReadError, page_texts, scan_pages


def highlight() -> DictionaryObject:
    return DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Highlight"),
            NameObject("/Rect"): ArrayObject([NumberObject(n) for n in (10, 10, 50, 50)]),
        }
    )


def write(path, annots_indirect: bool, count: int = 2):
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    page = writer.pages[0]
    array = ArrayObject([writer._add_object(highlight()) for _ in range(count)])
    page[NameObject("/Annots")] = writer._add_object(array) if annots_indirect else array
    with path.open("wb") as fh:
        writer.write(fh)
    return path


class TestScanPages:
    def test_counts_annotations_stored_as_a_direct_array(self, tmp_path):
        assert scan_pages(write(tmp_path / "direct.pdf", annots_indirect=False))[1] == 2

    def test_counts_annotations_stored_as_a_reference(self, tmp_path):
        """Both forms are valid PDF; calling len() on the reference raises TypeError."""
        assert scan_pages(write(tmp_path / "indirect.pdf", annots_indirect=True))[1] == 2

    def test_a_page_without_annotations_counts_zero(self, tmp_path):
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        path = tmp_path / "plain.pdf"
        with path.open("wb") as fh:
            writer.write(fh)
        texts, annots = scan_pages(path)
        assert annots == 0 and len(texts) == 1

    def test_a_non_pdf_is_reported_not_raised_raw(self, tmp_path):
        path = tmp_path / "broken.pdf"
        path.write_bytes(b"not a pdf at all")
        with pytest.raises(PdfReadError, match="broken.pdf"):
            page_texts(path)
