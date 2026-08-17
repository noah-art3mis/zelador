"""Tag taxonomy registry: families, canonical `family:value` tags, aliases, colours.

The registry is linted at load, before anything expands against it — a defective
registry fails loudly here, never downstream.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from zelador.config import TAXONOMY_FILE, ConfigError

COLOURED_CAP = 9  # Zotero pins at most 9 coloured tags

_TAG_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*:[a-z0-9][a-z0-9-]*$")
_HEX_COLOUR = re.compile(r"^#[0-9A-Fa-f]{6}$")


class TaxonomyError(ConfigError):
    """Defective registry file."""


@dataclass(frozen=True)
class Family:
    description: str = ""
    coloured: bool = False
    exclusive: bool = False  # at most one tag of this family per item


@dataclass(frozen=True)
class TagEntry:
    tag: str
    description: str = ""
    aliases: tuple[str, ...] = ()
    colour: str | None = None

    @property
    def family(self) -> str:
        return self.tag.split(":", 1)[0]


@dataclass(frozen=True)
class Taxonomy:
    families: dict[str, Family] = field(default_factory=dict)
    tags: tuple[TagEntry, ...] = ()

    def canonical(self) -> set[str]:
        return {t.tag for t in self.tags}

    def alias_map(self) -> dict[str, str]:
        """alias → canonical tag; lint guarantees each alias has one owner."""
        return {alias: t.tag for t in self.tags for alias in t.aliases}

    def is_known(self, name: str) -> bool:
        return name in self.canonical() or name in self.alias_map()

    def coloured(self) -> list[TagEntry]:
        return [t for t in self.tags if t.colour]

    def tag_colors_value(self) -> list[dict]:
        """The authoritative tagColors setting value; position = declared order."""
        return [{"name": t.tag, "color": t.colour} for t in self.coloured()]


def load_taxonomy(path: Path = TAXONOMY_FILE) -> Taxonomy:
    """Parse and lint the registry file."""
    if not path.exists():
        raise TaxonomyError(f"no taxonomy at {path} — copy taxonomy.example.yaml to start one")
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or not isinstance(raw.get("families"), dict):
        raise TaxonomyError(f"{path} must be a mapping with a 'families' mapping")
    if not isinstance(raw.get("tags"), list):
        raise TaxonomyError(f"{path} must declare a 'tags' list")
    families = {}
    for name, spec in raw["families"].items():
        spec = _mapping(
            spec or {}, f"family {name!r}", {"description", "coloured", "exclusive"}, path
        )
        families[str(name)] = Family(
            description=str(spec.get("description", "")),
            coloured=bool(spec.get("coloured", False)),
            exclusive=bool(spec.get("exclusive", False)),
        )
    entries = []
    for entry in raw["tags"]:
        entry = _mapping(entry, "tag entry", {"tag", "description", "aliases", "colour"}, path)
        entries.append(
            TagEntry(
                tag=str(entry.get("tag", "")),
                description=str(entry.get("description", "")),
                aliases=tuple(str(a) for a in entry.get("aliases") or ()),
                colour=entry.get("colour"),
            )
        )
    taxonomy = Taxonomy(families=families, tags=tuple(entries))
    _lint(taxonomy, path)
    return taxonomy


def _mapping(value, what: str, allowed: set[str], path: Path) -> dict:
    """A registry node must be a mapping with only known keys — typos fail loudly."""
    if not isinstance(value, dict):
        raise TaxonomyError(f"{path}: {what} must be a mapping, got {value!r}")
    unknown = set(value) - allowed
    if unknown:
        raise TaxonomyError(
            f"{path}: unknown key(s) on {what}: {', '.join(sorted(unknown))} "
            f"— allowed: {', '.join(sorted(allowed))}"
        )
    return value


def _lint(taxonomy: Taxonomy, path: Path) -> None:
    seen: set[str] = set()
    for t in taxonomy.tags:
        if not _TAG_NAME.match(t.tag):
            raise TaxonomyError(f"{path}: {t.tag!r} is not a lowercase family:value tag")
        if t.family not in taxonomy.families:
            raise TaxonomyError(f"{path}: tag {t.tag!r} uses undeclared family {t.family!r}")
        if t.tag in seen:
            raise TaxonomyError(f"{path}: canonical tag {t.tag!r} is duplicated")
        seen.add(t.tag)
    owner: dict[str, str] = {}
    for t in taxonomy.tags:
        for alias in t.aliases:
            if alias in seen:
                raise TaxonomyError(f"{path}: alias {alias!r} shadows a canonical tag")
            if alias in owner:
                raise TaxonomyError(
                    f"{path}: alias {alias!r} listed under both {owner[alias]!r} and {t.tag!r}"
                )
            owner[alias] = t.tag
    coloured = taxonomy.coloured()
    for t in coloured:
        if not isinstance(t.colour, str) or not _HEX_COLOUR.match(t.colour):
            raise TaxonomyError(
                f"{path}: colour {t.colour!r} on {t.tag!r} is not a #RRGGBB hex colour"
            )
        if not taxonomy.families[t.family].coloured:
            raise TaxonomyError(
                f"{path}: {t.tag!r} has a colour but family {t.family!r} is not coloured"
            )
    if len(coloured) > COLOURED_CAP:
        raise TaxonomyError(
            f"{path}: {len(coloured)} coloured tags — Zotero pins at most {COLOURED_CAP}"
        )
