"""Expansion: validate symbolic intents and expand them into a version-pinned plan.

Every failure is collected, never raised mid-walk — the agent gets the whole
list at once. Operations on the same object compose through a working copy of
its state, so one plan produces one write per object and the second operation's
old state is the first's new state.
"""

from __future__ import annotations

import copy
import secrets
from collections.abc import Callable
from datetime import datetime

from zelador.client import ZoteroClient, ZoteroError
from zelador.taxonomy import Taxonomy
from zelador.write.contracts import Changeset, Operation, Plan, plan_id

ZOTERO_KEY_ALPHABET = "23456789ABCDEFGHIJKLMNPQRSTUVWXYZ"


class ValidationError(Exception):
    """One or more validation failures; nothing expands."""

    def __init__(self, failures: list[str]):
        self.failures = failures
        super().__init__("\n".join(failures))


def _generate_key() -> str:
    return "".join(secrets.choice(ZOTERO_KEY_ALPHABET) for _ in range(8))


def expand(
    changeset: Changeset,
    client: ZoteroClient,
    taxonomy: Taxonomy | None,
    backup: str,
    now: datetime,
    keygen: Callable[[], str] | None = None,
) -> Plan:
    """Expand a changeset against the live library; raises ValidationError on any failure."""
    items = client.all_items()
    collections = client.all_collections()
    # Captured before the settings GET — a single-object request whose header
    # carries the setting's own version, not the library version.
    library_version = client.last_modified_version
    assert library_version is not None
    tag_colors = client.setting("tagColors")
    expander = _Expander(client, items, collections, taxonomy, keygen or _generate_key)
    for group, intent in enumerate(changeset.intents):
        expander.expand_intent(group, intent)
    expander.check_exclusivity()
    if expander.failures:
        raise ValidationError(expander.failures)
    return Plan(
        id=plan_id(changeset.slug, now),
        slug=changeset.slug,
        backup=backup,
        library_version=library_version,
        intents=changeset.intents,
        operations=expander.operations,
        settings=_settings_drift(taxonomy, tag_colors, library_version),
    )


def _settings_drift(taxonomy: Taxonomy | None, live_setting, library_version: int) -> dict | None:
    """The registry is authoritative for the whole tagColors array — any difference is drift."""
    if taxonomy is None:
        return None
    live = live_setting["value"] if live_setting else []
    registry = taxonomy.tag_colors_value()
    if live == registry:
        return None
    return {"name": "tagColors", "version": library_version, "old": live, "new": registry}


class _Expander:
    def __init__(self, client, items, collections, taxonomy, keygen):
        self.client = client
        self.taxonomy = taxonomy
        self.keygen = keygen
        self.items = items
        self.items_by_key = {i["key"]: i for i in items}
        self.colls_by_key = {c["key"]: c for c in collections}
        self.created_keys: set[str] = set()
        self._working_items: dict[str, dict] = {}
        self._working_colls: dict[str, dict] = {}
        self.operations: list[Operation] = []
        self.failures: list[str] = []

    # -- state ---------------------------------------------------------------

    def item_state(self, key: str) -> dict:
        """The item's evolving data — ops later in the changeset see earlier results."""
        if key not in self._working_items:
            self._working_items[key] = copy.deepcopy(self.items_by_key[key]["data"])
        return self._working_items[key]

    def coll_state(self, key: str) -> dict:
        if key not in self._working_colls:
            self._working_colls[key] = copy.deepcopy(self.colls_by_key[key]["data"])
        return self._working_colls[key]

    def item_tags(self, key: str) -> list[dict]:
        if key in self._working_items:
            return self._working_items[key]["tags"]
        return self.items_by_key[key]["data"].get("tags", [])

    def emit(self, group, op, kind, key, version, facet, old, new, risk) -> None:
        operation = Operation(
            id=f"op-{len(self.operations) + 1:03d}",
            group=group,
            op=op,
            kind=kind,
            key=key,
            version=version,
            facet=facet,
            old=old,
            new=new,
            risk=risk,
        )
        self.operations.append(operation)

    def fail(self, where: str, message: str) -> None:
        self.failures.append(f"{where}: {message}")

    def require_item(self, key: str, where: str) -> dict | None:
        if key not in self.items_by_key:
            self.fail(where, f"no such item: {key}")
            return None
        return self.item_state(key)

    def require_collection(self, key: str, where: str) -> dict | None:
        if key not in self.colls_by_key:
            self.fail(where, f"no such collection: {key}")
            return None
        return self.coll_state(key)

    # -- dispatch ------------------------------------------------------------

    def expand_intent(self, group: int, intent: dict) -> None:
        op = intent["op"]
        where = f"intent {group} ({op})"
        getattr(self, f"_{op}")(group, where, intent)

    def _canonical_tag(self, tag: str, where: str) -> bool:
        """True when `tag` may be written. Aliases point at their canonical owner."""
        if self.taxonomy is None:
            self.fail(where, "writing tags requires taxonomy.yaml")
            return False
        if tag in self.taxonomy.canonical():
            return True
        owner = self.taxonomy.alias_map().get(tag)
        if owner:
            self.fail(where, f"{tag!r} is an alias — the canonical tag is {owner!r}")
        else:
            self.fail(where, f"{tag!r} is not a canonical registry tag")
        return False

    # -- tag ops -------------------------------------------------------------

    def _merge_tag(self, group: int, where: str, intent: dict) -> None:
        sources, target = intent["from"], intent["into"]
        if not self._canonical_tag(target, where):
            return
        merged_canonical = [t for t in sources if t in self.taxonomy.canonical()]
        if merged_canonical:
            self.fail(where, f"cannot merge canonical tag(s) away: {', '.join(merged_canonical)}")
            return
        source_set = set(sources)
        for item in self.items:
            key = item["key"]
            tags = self.item_tags(key)
            if not any(t["tag"] in source_set for t in tags):
                continue
            new = [t for t in tags if t["tag"] not in source_set]
            if not any(t["tag"] == target for t in new):
                new.append({"tag": target, "type": 0})
            self.emit(group, "merge_tag", "item", key, item["version"], "tags",
                      copy.deepcopy(tags), new, "high")
            self.item_state(key)["tags"] = new

    def _add_tag(self, group: int, where: str, intent: dict) -> None:
        tag = intent["tag"]
        if not self._canonical_tag(tag, where):
            return
        for key in intent["keys"]:
            if self.require_item(key, where) is None:
                continue
            tags = self.item_tags(key)
            if any(t["tag"] == tag for t in tags):
                continue
            new = [*copy.deepcopy(tags), {"tag": tag, "type": 0}]
            version = self.items_by_key[key]["version"]
            self.emit(group, "add_tag", "item", key, version, "tags",
                      copy.deepcopy(tags), new, "low")
            self.item_state(key)["tags"] = new

    def _remove_tag(self, group: int, where: str, intent: dict) -> None:
        tag = intent["tag"]
        for key in intent["keys"]:
            if self.require_item(key, where) is None:
                continue
            tags = self.item_tags(key)
            if not any(t["tag"] == tag for t in tags):
                continue
            new = [t for t in copy.deepcopy(tags) if t["tag"] != tag]
            version = self.items_by_key[key]["version"]
            self.emit(group, "remove_tag", "item", key, version, "tags",
                      copy.deepcopy(tags), new, "high")
            self.item_state(key)["tags"] = new

    # -- item ops ------------------------------------------------------------

    def _fill_field(self, group: int, where: str, intent: dict) -> None:
        self._write_field(group, where, intent["key"], intent["field"], intent["value"],
                          op="fill_field")

    def _clear_field(self, group: int, where: str, intent: dict) -> None:
        self._write_field(group, where, intent["key"], intent["field"], "", op="clear_field")

    def _write_field(self, group: int, where, key, field, value, op: str) -> None:
        data = self.require_item(key, where)
        if data is None:
            return
        if field == "extra":
            self.fail(where, f"'extra' is never a {op} target — it is plugin territory "
                             "(Better BibTeX pins citekeys there)")
            return
        valid = self.client.item_type_fields(data["itemType"])
        if field not in valid:
            self.fail(where, f"{field!r} is not a field of {data['itemType']!r}")
            return
        old = data.get(field) or ""
        if old == value:
            return
        version = self.items_by_key[key]["version"]
        risk = "low" if not old else "high"
        self.emit(group, op, "item", key, version, f"field:{field}", old, value, risk)
        data[field] = value

    def _set_creators(self, group: int, where: str, intent: dict) -> None:
        key, creators = intent["key"], intent["creators"]
        data = self.require_item(key, where)
        if data is None:
            return
        valid = self.client.item_type_creator_types(data["itemType"])
        for creator in creators:
            if creator["creatorType"] not in valid:
                self.fail(where, f"{creator['creatorType']!r} is not a creator type "
                                 f"of {data['itemType']!r}")
                return
        old = data.get("creators", [])
        if old == creators:
            return
        version = self.items_by_key[key]["version"]
        risk = "low" if not old else "high"
        self.emit(group, "set_creators", "item", key, version, "creators", old, creators, risk)
        data["creators"] = creators

    def _create_item(self, group: int, where: str, intent: dict) -> None:
        item_type = intent["itemType"]
        try:
            valid = self.client.item_type_fields(item_type)
        except ZoteroError:
            self.fail(where, f"{item_type!r} is not a valid item type")
            return
        ok = self._check_create_fields(where, item_type, valid, intent)
        ok = self._check_create_tags(where, intent.get("tags", [])) and ok
        for ckey in intent.get("collections", []):
            if ckey not in self.colls_by_key:
                self.fail(where, f"no such collection: {ckey}")
                ok = False
        attachment = intent.get("attachment")
        if attachment is not None:
            ok = self._check_adoptable(where, attachment) and ok
        if not ok:
            return
        # An adopted attachment hands over everything the user said about the file.
        # Zotero surfaces tags on items and forbids child items in collections, so
        # anything left on the child is invisible or silently dropped. Adopted tags
        # skip the registry check on purpose: they already exist in the library, and
        # the registry governs what may be *proposed*, not what is already assigned.
        adopted_tags, adopted_colls = [], []
        if attachment is not None:
            adata = self.item_state(attachment)
            adopted_tags = [t["tag"] for t in adata.get("tags", [])]
            adopted_colls = list(adata.get("collections", []))
        tags = list(dict.fromkeys([*intent.get("tags", []), *adopted_tags]))
        colls = list(dict.fromkeys([*intent.get("collections", []), *adopted_colls]))

        key = self.keygen()
        new = {"itemType": item_type, **intent["fields"],
               "tags": [{"tag": t, "type": 0} for t in tags],
               "collections": colls}
        if intent.get("creators"):
            new["creators"] = intent["creators"]
        self.emit(group, "create_item", "item", key, 0, "object", None, new, "low")
        self.items_by_key[key] = {"key": key, "version": 0,
                                  "data": {**copy.deepcopy(new), "key": key}}
        self.created_keys.add(key)
        if attachment is not None:
            version = self.items_by_key[attachment]["version"]
            adata = self.item_state(attachment)
            self.emit(group, "create_item", "item", attachment, version,
                      "parentItem", False, key, "low")
            adata["parentItem"] = key
            if adopted_tags:
                self.emit(group, "create_item", "item", attachment, version,
                          "tags", adata.get("tags", []), [], "low")
                adata["tags"] = []
            if adopted_colls:
                self.emit(group, "create_item", "item", attachment, version,
                          "collections", adopted_colls, [], "low")
                adata["collections"] = []

    def _check_create_fields(self, where, item_type, valid, intent) -> bool:
        ok = True
        for name in intent["fields"]:
            if name == "extra":
                self.fail(where, "'extra' is never a create_item target — it is plugin "
                                 "territory (Better BibTeX pins citekeys there)")
                ok = False
            elif name not in valid:
                self.fail(where, f"{name!r} is not a field of {item_type!r}")
                ok = False
        for creator in intent.get("creators", []):
            if creator["creatorType"] not in self.client.item_type_creator_types(item_type):
                self.fail(where, f"{creator['creatorType']!r} is not a creator type "
                                 f"of {item_type!r}")
                ok = False
        return ok

    def _check_create_tags(self, where, tags: list[str]) -> bool:
        ok = all([self._canonical_tag(tag, where) for tag in tags])
        if self.taxonomy is None:
            return ok
        for family, spec in self.taxonomy.families.items():
            if spec.exclusive and sum(t.startswith(f"{family}:") for t in tags) > 1:
                self.fail(where, f"would carry multiple '{family}:' tags — family is exclusive")
                ok = False
        return ok

    def _check_adoptable(self, where, attachment: str) -> bool:
        if attachment not in self.items_by_key:
            self.fail(where, f"no such item: {attachment}")
            return False
        data = self.item_state(attachment)
        if data["itemType"] != "attachment":
            self.fail(where, f"{attachment} is not an attachment")
            return False
        if data.get("parentItem"):
            self.fail(where, f"{attachment} is already attached to {data['parentItem']}")
            return False
        return True

    def _set_item_type(self, group: int, where: str, intent: dict) -> None:
        key, new_type = intent["key"], intent["itemType"]
        data = self.require_item(key, where)
        if data is None:
            return
        old_type = data["itemType"]
        if old_type == new_type:
            return
        try:
            new_valid = self.client.item_type_fields(new_type)
        except ZoteroError:
            self.fail(where, f"{new_type!r} is not a valid item type")
            return
        bad_creators = sorted(
            {c["creatorType"] for c in data.get("creators", [])}
            - self.client.item_type_creator_types(new_type)
        )
        if bad_creators:
            self.fail(where, f"creator type(s) {', '.join(bad_creators)} not valid for "
                             f"{new_type!r} — set_creators first")
            return
        old_valid = self.client.item_type_fields(old_type)
        version = self.items_by_key[key]["version"]
        self.emit(group, "set_item_type", "item", key, version, "itemType",
                  old_type, new_type, "high")
        data["itemType"] = new_type
        # The server validates the merged object against the new type: stored
        # fields it no longer allows must be cleared in the same write.
        for name in sorted(set(data) & old_valid - new_valid):
            if not data.get(name):
                continue
            self.emit(group, "set_item_type", "item", key, version, f"field:{name}",
                      data[name], "", "high")
            data[name] = ""

    def _trash_item(self, group: int, where: str, intent: dict) -> None:
        key = intent["key"]
        data = self.require_item(key, where)
        if data is None or data.get("deleted"):
            return
        version = self.items_by_key[key]["version"]
        self.emit(group, "trash_item", "item", key, version, "deleted", False, True, "high")
        data["deleted"] = True

    # -- collection membership -----------------------------------------------

    def _add_to_collection(self, group: int, where: str, intent: dict) -> None:
        self._membership(group, where, intent, add=True)

    def _remove_from_collection(self, group: int, where: str, intent: dict) -> None:
        self._membership(group, where, intent, add=False)

    def _membership(self, group: int, where: str, intent: dict, add: bool) -> None:
        ckey = intent["collection"]
        if ckey not in self.colls_by_key:
            self.fail(where, f"no such collection: {ckey}")
            return
        for key in intent["keys"]:
            data = self.require_item(key, where)
            if data is None:
                continue
            memberships = data.get("collections", [])
            if add == (ckey in memberships):
                continue
            new = [*memberships, ckey] if add else [m for m in memberships if m != ckey]
            version = self.items_by_key[key]["version"]
            op = "add_to_collection" if add else "remove_from_collection"
            self.emit(group, op, "item", key, version, "collections",
                      list(memberships), new, "low" if add else "high")
            data["collections"] = new

    # -- collection objects --------------------------------------------------

    def _create_collection(self, group: int, where: str, intent: dict) -> None:
        parent = intent.get("parent")
        if parent is not None and parent not in self.colls_by_key:
            self.fail(where, f"no such parent collection: {parent}")
            return
        key = self.keygen()
        new = {"name": intent["name"], "parentCollection": parent or False}
        self.emit(group, "create_collection", "collection", key, 0, "object", None, new, "low")
        self.colls_by_key[key] = {"key": key, "version": 0, "data": {**new, "key": key}}
        self.created_keys.add(key)

    def _rename_collection(self, group: int, where: str, intent: dict) -> None:
        key = intent["collection"]
        data = self.require_collection(key, where)
        if data is None or data["name"] == intent["name"]:
            return
        version = self.colls_by_key[key]["version"]
        self.emit(group, "rename_collection", "collection", key, version, "name",
                  data["name"], intent["name"], "low")
        data["name"] = intent["name"]

    def _move_collection(self, group: int, where: str, intent: dict) -> None:
        key = intent["collection"]
        data = self.require_collection(key, where)
        if data is None:
            return
        parent = intent["parent"]
        if parent is not None:
            if parent not in self.colls_by_key:
                self.fail(where, f"no such parent collection: {parent}")
                return
            if self._is_descendant(parent, of=key):
                self.fail(where, f"{parent} is a descendant of {key} — the move would cycle")
                return
        new = parent or False
        if data["parentCollection"] == new:
            return
        version = self.colls_by_key[key]["version"]
        self.emit(group, "move_collection", "collection", key, version, "parentCollection",
                  data["parentCollection"], new, "high")
        data["parentCollection"] = new

    def _trash_collection(self, group: int, where: str, intent: dict) -> None:
        key = intent["collection"]
        data = self.require_collection(key, where)
        if data is None or data.get("deleted"):
            return
        version = self.colls_by_key[key]["version"]
        self.emit(group, "trash_collection", "collection", key, version, "deleted",
                  False, True, "high")
        data["deleted"] = True

    def _is_descendant(self, key: str, of: str) -> bool:
        """Walk parent links in working state; `of` reachable from `key` means descendant."""
        seen = set()
        current = key
        while current and current not in seen:
            seen.add(current)
            if current == of:
                return True
            coll = self._working_colls.get(current) or self.colls_by_key[current]["data"]
            current = coll.get("parentCollection") or None
        return False

    # -- cross-cutting checks ------------------------------------------------

    def check_exclusivity(self) -> None:
        """A write may not introduce a new exclusive-family violation; stock violations
        it leaves untouched are the conformance audit's business, not a veto."""
        if self.taxonomy is None:
            return
        exclusive = [f for f, spec in self.taxonomy.families.items() if spec.exclusive]
        for key, working in self._working_items.items():
            original = self.items_by_key[key]["data"].get("tags", [])
            for family in exclusive:
                before = sum(1 for t in original if t["tag"].startswith(f"{family}:"))
                after = sum(1 for t in working["tags"] if t["tag"].startswith(f"{family}:"))
                if after > 1 and after > before:
                    self.failures.append(
                        f"item {key}: would carry {after} '{family}:' tags — family is exclusive"
                    )
