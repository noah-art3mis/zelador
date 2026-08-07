"""Fulltext of an item's PDF: server extraction first, local pypdf as fallback.

The result always carries the full text plus a metadata-sized head — a real
first page locally, a character budget for the server's unpaginated string —
so truncation stays display-time and escalating to --full costs nothing.
Server responses go through the never-expiring lookup cache. `render_page`
rasterizes page one for scanned PDFs, importing pypdfium2/Pillow lazily so
the text paths never need them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from zelador.client import ZoteroClient
from zelador.lookup.cache import LookupCache
from zelador.lookup.sources import SourceError

PDF_MIME = "application/pdf"
HEAD_CHARS = 4000  # server head budget, sized like a dense first page


@dataclass(frozen=True)
class Fulltext:
    item: str  # the requested key
    attachment: str  # the resolved PDF attachment key
    origin: str  # "server" | "local"
    content: str  # the whole text
    head: str  # first page (local) or the first HEAD_CHARS (server)
    pages: int | None  # total pages when known
    path: str | None  # the PDF's on-disk location when resolvable


def resolve_attachment(client: ZoteroClient, key: str) -> dict:
    """The PDF attachment behind a key — the key itself, or the item's first PDF child."""
    found = client.items_batch([key])
    if not found:
        raise SourceError(f"no such item: {key}")
    item = found[0]
    if item["data"]["itemType"] == "attachment":
        return item
    pdfs = [c for c in client.children(key) if c["data"].get("contentType") == PDF_MIME]
    if not pdfs:
        raise SourceError(f"item {key} has no PDF attachment")
    return sorted(pdfs, key=lambda c: c["key"])[0]


def attachment_pdf_path(zotero_dir: Path, attachment: dict) -> Path:
    filename = attachment["data"].get("filename", "")
    path = zotero_dir / "storage" / attachment["key"] / filename
    if not filename or not path.exists():
        raise SourceError(
            f"attachment {attachment['key']} has no local file under {zotero_dir}/storage"
        )
    return path


def fetch_fulltext(
    client: ZoteroClient,
    key: str,
    zotero_dir: Path | None,
    cache: LookupCache | None = None,
) -> Fulltext:
    return attachment_fulltext(client, key, resolve_attachment(client, key), zotero_dir, cache)


def attachment_fulltext(
    client: ZoteroClient,
    key: str,
    attachment: dict,
    zotero_dir: Path | None,
    cache: LookupCache | None = None,
) -> Fulltext:
    """Fulltext of an already-resolved attachment; `key` names what was asked for."""
    local_path = _local_path(zotero_dir, attachment)
    body = _server_body(client, attachment["key"], cache)
    if body and body.get("content"):
        content = body["content"]
        return Fulltext(
            item=key,
            attachment=attachment["key"],
            origin="server",
            content=content,
            head=content[:HEAD_CHARS],
            pages=body.get("totalPages") or body.get("indexedPages"),
            path=local_path,
        )
    if zotero_dir is None:
        raise SourceError(
            f"server has no fulltext for {attachment['key']} and no local Zotero "
            "data dir is available for pypdf extraction"
        )
    pages = _extract_pages(attachment_pdf_path(zotero_dir, attachment))
    return Fulltext(
        item=key,
        attachment=attachment["key"],
        origin="local",
        content="\n".join(pages).strip(),
        head=pages[0].strip() if pages else "",
        pages=len(pages),
        path=local_path,
    )


def _server_body(client: ZoteroClient, attachment_key: str, cache: LookupCache | None):
    """The server fulltext response, cache-through; misses are never cached."""
    url = f"zotero:/items/{attachment_key}/fulltext"
    if cache is not None:
        cached = cache.get(url)
        if cached is not None:
            return json.loads(cached)
    body = client.fulltext(attachment_key)
    if cache is not None and body and body.get("content"):
        cache.put(url, json.dumps(body, ensure_ascii=False))
    return body


def _local_path(zotero_dir: Path | None, attachment: dict) -> str | None:
    if zotero_dir is None:
        return None
    try:
        return str(attachment_pdf_path(zotero_dir, attachment))
    except SourceError:
        return None


def _extract_pages(pdf_path: Path) -> list[str]:
    from zelador.pdf import PdfReadError, page_texts

    try:
        return page_texts(pdf_path)
    except PdfReadError as exc:
        raise SourceError(str(exc)) from None


def render_page(pdf_path: Path, out_path: Path) -> Path:
    """Rasterize page one to PNG (2x scale) for eyeballing scanned PDFs."""
    import pypdfium2

    try:
        document = pypdfium2.PdfDocument(pdf_path)
        page = document[0]
        page.render(scale=2.0).to_pil().save(out_path, format="PNG")
    except Exception as exc:
        raise SourceError(f"could not render {pdf_path}: {exc}") from None
    return out_path
