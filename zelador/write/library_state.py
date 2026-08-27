"""Shared current-state helpers for verification, undo, and recovery.

One vocabulary for reading an operation's facet out of live object data, and
one fetch shape for pulling exactly the touched objects back from the API.
"""

from __future__ import annotations

from zelador.client import ZoteroClient


def facet_field(facet: str) -> str:
    """The object data field a facet writes to ("field:volume" -> "volume")."""
    return facet.removeprefix("field:") if facet.startswith("field:") else facet


_SET_FACETS = ("tags", "collections", "creators")
_PARENT_FACETS = ("parentCollection", "parentItem")


def live_value(data: dict, name: str):
    """One written field's live value under the server's serialization: a
    set-valued field reads as empty when the server omits or nulls it (Zotero
    carries no `collections` on child items at all), a parent pointer as False,
    `deleted` as a bool. Compare the result with state_equal."""
    if name in _SET_FACETS:
        return data.get(name) or []
    if name in _PARENT_FACETS:
        return data.get(name, False)
    if name == "deleted":
        return bool(data.get(name))
    return data.get(name)


def facet_value(data: dict, facet: str):
    """The current value of an operation's facet, read from an object's data."""
    if facet.startswith("field:"):
        return data.get(facet_field(facet)) or ""
    if facet in (*_SET_FACETS, *_PARENT_FACETS, "deleted", "name", "itemType"):
        return live_value(data, facet)
    raise ValueError(f"unknown facet: {facet}")


def state_equal(facet: str, a, b) -> bool:
    """Facet-state equality under the server's serialization: an item's tags and
    its collection membership are both sets — Zotero stores them re-sorted and
    omits a manual tag's type, so neither order nor an absent type 0
    distinguishes two states."""
    if facet == "collections":
        return sorted(a) == sorted(b)
    if facet == "tags":
        return sorted((t["tag"], t.get("type", 0)) for t in a) == sorted(
            (t["tag"], t.get("type", 0)) for t in b
        )
    return a == b


def setting_value(setting: dict | None) -> list:
    """A settings GET result flattened to its comparable value; unset means []."""
    return setting["value"] if setting else []


def fetch_objects(
    client: ZoteroClient, item_keys: list[str], coll_keys: list[str]
) -> dict[tuple[str, str], dict]:
    """Exactly the named objects (trash included), keyed by (kind, key)."""
    current: dict[tuple[str, str], dict] = {}
    if item_keys:
        for obj in client.items_batch(sorted(item_keys), include_trashed=True):
            current[("item", obj["key"])] = obj
    if coll_keys:
        for obj in client.collections_batch(sorted(coll_keys)):
            current[("collection", obj["key"])] = obj
    return current
