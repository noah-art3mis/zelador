"""Shared fixtures: an in-memory fake of the Zotero Web API served over httpx.MockTransport."""

from __future__ import annotations

import json
import re
from collections import deque
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

USER_ID = "11868292"


def make_item(
    key: str,
    version: int = 1,
    item_type: str = "journalArticle",
    title: str = "A title",
    deleted: bool = False,
    parsed_date: str | None = None,
    bib: str | None = None,
    **data,
):
    payload = {"key": key, "version": version, "itemType": item_type, "title": title}
    payload.setdefault("tags", [])
    payload.setdefault("collections", [])
    payload.update(data)
    if deleted:
        payload["deleted"] = 1
    item = {
        "key": key,
        "version": version,
        "library": {"type": "user"},
        "meta": {"parsedDate": parsed_date} if parsed_date else {},
        "data": payload,
    }
    if bib is not None:
        item["bib"] = bib
    return item


def make_collection(key: str, name: str, version: int = 1, parent: str | None = None):
    return {
        "key": key,
        "version": version,
        "meta": {},
        "data": {
            "key": key,
            "version": version,
            "name": name,
            "parentCollection": parent or False,
            "deleted": False,
        },
    }


def make_tag(tag: str, num_items: int = 1, tag_type: int = 0):
    return {"tag": tag, "meta": {"type": tag_type, "numItems": num_items}}


# The subset of /itemTypeFields the tests exercise.
ITEM_TYPE_FIELDS = {
    "journalArticle": [
        "title",
        "abstractNote",
        "publicationTitle",
        "volume",
        "issue",
        "pages",
        "date",
        "DOI",
        "url",
        "extra",
    ],
    "book": ["title", "abstractNote", "publisher", "date", "url", "extra"],
    "webpage": ["title", "websiteTitle", "date", "url", "extra"],
    "conferencePaper": [
        "title",
        "abstractNote",
        "proceedingsTitle",
        "conferenceName",
        "pages",
        "date",
        "DOI",
        "url",
        "extra",
    ],
}

# The subset of /itemTypeCreatorTypes the tests exercise.
ITEM_TYPE_CREATOR_TYPES = {
    "journalArticle": ["author", "contributor", "editor"],
    "book": ["author", "contributor", "editor"],
    "webpage": ["author", "contributor"],
    "conferencePaper": ["author", "contributor", "editor"],
}

# Item data keys that are not per-type fields and always pass server validation.
ITEM_BASE_KEYS = {
    "key",
    "version",
    "itemType",
    "creators",
    "tags",
    "collections",
    "relations",
    "deleted",
    "dateAdded",
    "dateModified",
    "parentItem",
    "linkMode",
    "contentType",
    "filename",
    "charset",
    "note",
}


class FakeZotero:
    """Minimal stand-in for api.zotero.org: pagination, versions, backoff scripting."""

    def __init__(
        self,
        items: list | None = None,
        collections: list | None = None,
        tags: list | None = None,
        settings: dict | None = None,
        library_version: int = 42,
        page_size: int = 2,
        emit_next_links: bool = True,
        children: dict | None = None,
        fulltexts: dict | None = None,
    ):
        self.children = children or {}  # parent key -> child items
        self.fulltexts = fulltexts or {}  # attachment key -> fulltext response body
        self.items = items or []
        self.collections = collections or []
        self.tags = tags or []
        self.settings = settings or {}
        self.library_version = library_version
        self.page_size = page_size
        self.emit_next_links = emit_next_links  # False mimics /tags' broken metadata
        self.requests: list[httpx.Request] = []
        self.script: deque[httpx.Response] = deque()  # scripted responses jump the queue

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            return self.script.popleft()
        parsed = urlparse(str(request.url))
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        path = parsed.path

        if path == "/keys/current":
            return self._json(
                {
                    "userID": int(USER_ID),
                    "username": "noah-art3mis",
                    "access": {"user": {"library": True, "write": True}},
                }
            )
        if path == "/itemTypeFields":
            fields = ITEM_TYPE_FIELDS.get(params.get("itemType", ""))
            if fields is None:
                return httpx.Response(400, text="Invalid item type")
            return self._json([{"field": f, "localized": f} for f in fields])
        if path == "/itemTypeCreatorTypes":
            creator_types = ITEM_TYPE_CREATOR_TYPES.get(params.get("itemType", ""))
            if creator_types is None:
                return httpx.Response(400, text="Invalid item type")
            return self._json([{"creatorType": c, "localized": c} for c in creator_types])
        if path == f"/users/{USER_ID}/items":
            if request.method == "POST":
                return self._write(request, self.items, "item")
            return self._items_response(request, params, path)
        children = re.fullmatch(f"/users/{USER_ID}/items/(\\w+)/children", path)
        if children:
            return self._paginated(self.children.get(children.group(1), []), params, path)
        fulltext = re.fullmatch(f"/users/{USER_ID}/items/(\\w+)/fulltext", path)
        if fulltext:
            if fulltext.group(1) in self.fulltexts:
                return self._json(self.fulltexts[fulltext.group(1)])
            return httpx.Response(404, text="Not found")
        if path == f"/users/{USER_ID}/collections":
            if request.method == "POST":
                return self._write(request, self.collections, "collection")
            pool = self.collections
            if "collectionKey" in params:
                keys = params["collectionKey"].split(",")
                pool = [c for c in pool if c["key"] in keys]
            return self._paginated(pool, params, path)
        if path == f"/users/{USER_ID}/tags":
            return self._paginated(self.tags, params, path)
        if path.startswith(f"/users/{USER_ID}/settings/"):
            name = path.rsplit("/", 1)[1]
            if request.method == "PUT":
                return self._write_setting(request, name)
            if name in self.settings:
                # Single-object request: the header carries the setting's own
                # version, not the library version (matches the live API).
                setting = self.settings[name]
                return httpx.Response(
                    200,
                    json=setting,
                    headers={"Last-Modified-Version": str(setting["version"])},
                )
            return httpx.Response(404, text="Not found")
        return httpx.Response(404, text=f"no fake route for {path}")

    def _write(self, request: httpx.Request, pool: list, kind: str) -> httpx.Response:
        """POST batch write: partial updates and creates, per-object result maps."""
        by_key = {o["key"]: o for o in pool}
        new_version = self.library_version + 1
        success: dict = {}
        unchanged: dict = {}
        failed: dict = {}
        for idx, obj in enumerate(json.loads(request.content)):
            key = obj["key"]
            existing = by_key.get(key)
            if existing is None:
                data = self._server_form({k: v for k, v in obj.items() if k != "version"})
                error = self._validate_item(data) if kind == "item" else None
                if error:
                    failed[str(idx)] = {"code": 400, "message": error}
                    continue
                data["version"] = new_version
                pool.append({"key": key, "version": new_version, "meta": {}, "data": data})
                success[str(idx)] = key
                continue
            if obj.get("version") != existing["version"]:
                failed[str(idx)] = {
                    "code": 412,
                    "message": f"{kind} {key} has been modified since specified version",
                }
                continue
            merged = self._server_form(
                {**existing["data"], **{k: v for k, v in obj.items() if k != "version"}}
            )
            error = self._validate_item(merged) if kind == "item" else None
            if error:
                failed[str(idx)] = {"code": 400, "message": error}
                continue
            if merged == existing["data"]:
                unchanged[str(idx)] = key
                continue
            existing["data"] = merged
            existing["version"] = new_version
            existing["data"]["version"] = new_version
            success[str(idx)] = key
        if success:
            self.library_version = new_version
        return httpx.Response(
            200,
            json={"success": success, "unchanged": unchanged, "failed": failed},
            headers=self._version_header(),
        )

    @staticmethod
    def _validate_item(data: dict) -> str | None:
        """The live server validates the merged object: every stored field must be
        valid for the (possibly just-changed) item type, creators included."""
        item_type = data.get("itemType")
        if item_type == "attachment":
            return None
        known = ITEM_TYPE_FIELDS.get(item_type or "")
        if known is None:
            return f"'{item_type}' is not a valid item type"
        for name in data:
            if name not in ITEM_BASE_KEYS and name not in known:
                return f"invalid field '{name}' for type '{item_type}'"
        for creator in data.get("creators", []):
            if creator.get("creatorType") not in ITEM_TYPE_CREATOR_TYPES.get(item_type, []):
                return (
                    f"invalid creator type '{creator.get('creatorType')}' "
                    f"for type '{item_type}'"
                )
        return None

    @staticmethod
    def _server_form(data: dict) -> dict:
        """Zotero stores tags and collections sorted, serializes manual tags without
        their default type, and drops fields written as the empty string (matches the
        live API)."""
        data = {k: v for k, v in data.items() if v != ""}
        if "collections" in data:
            data["collections"] = sorted(data["collections"])
        if "tags" in data:
            data["tags"] = sorted(
                (
                    {k: v for k, v in t.items() if not (k == "type" and v == 0)}
                    for t in data["tags"]
                ),
                key=lambda t: t["tag"],
            )
        return data

    def _write_setting(self, request: httpx.Request, name: str) -> httpx.Response:
        cond = request.headers.get("If-Unmodified-Since-Version")
        current = self.settings.get(name, {}).get("version", 0)
        if cond is not None and int(cond) < current:
            return httpx.Response(412, text="Object has been modified since specified version")
        self.library_version += 1
        value = json.loads(request.content)["value"]
        self.settings[name] = {"value": value, "version": self.library_version}
        return httpx.Response(204, headers=self._version_header())

    def _items_response(self, request: httpx.Request, params: dict, path: str) -> httpx.Response:
        cond = request.headers.get("If-Modified-Since-Version")
        if cond is not None and int(cond) >= self.library_version:
            return httpx.Response(304, headers=self._version_header())
        pool = self.items
        if params.get("includeTrashed") != "1":
            pool = [i for i in pool if not i["data"].get("deleted")]
        if "since" in params:
            pool = [i for i in pool if i["version"] > int(params["since"])]
        if "itemKey" in params:
            keys = params["itemKey"].split(",")
            pool = [i for i in pool if i["key"] in keys]
        if "bib" in params.get("include", ""):
            pool = [
                {**i, "bib": f'<div class="csl-bib-body">{i["data"].get("title", "")}</div>'}
                for i in pool
            ]
        return self._paginated(pool, params, path)

    def _paginated(self, pool: list, params: dict, path: str) -> httpx.Response:
        start = int(params.get("start", 0))
        limit = min(int(params.get("limit", self.page_size)), self.page_size)
        page = pool[start : start + limit]
        headers = self._version_header()
        headers["Total-Results"] = str(len(pool))
        if self.emit_next_links and start + limit < len(pool):
            carried = {k: v for k, v in params.items() if k not in ("start", "limit")}
            query = "".join(f"&{k}={v}" for k, v in carried.items())
            headers["Link"] = (
                f"<https://api.zotero.org{path}"
                f"?start={start + limit}&limit={limit}{query}>; rel=\"next\""
            )
        return httpx.Response(200, json=page, headers=headers)

    def _version_header(self) -> dict:
        return {"Last-Modified-Version": str(self.library_version)}

    def _json(self, obj) -> httpx.Response:
        return httpx.Response(200, json=obj, headers=self._version_header())


@pytest.fixture
def fake():
    return FakeZotero()


def response_json(response_text: str) -> list:
    return json.loads(response_text)
