"""Configuration: credentials, data directories, config.yaml, Zotero data dir discovery."""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from pathlib import Path

import platformdirs
import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = REPO_ROOT / "config.yaml"
TAXONOMY_FILE = REPO_ROOT / "taxonomy.yaml"
WINDOWS_USERS_ROOT = Path("/mnt/c/Users")

DATA_SUBDIRS = ("backups", "audit", "changesets", "plans", "cache", "log", "compress")


class ConfigError(Exception):
    """Bad or missing configuration — always fails loudly."""


@dataclass(frozen=True)
class Credentials:
    api_key: str
    user_id: str


@dataclass(frozen=True)
class Config:
    zotero_data_dir: Path | None = None
    citekey_sources: list[str] = field(default_factory=list)
    style: str = "apa"


def load_credentials(dotenv_path: Path | None = None) -> Credentials:
    """Read API credentials from the environment (.env at repo root is loaded first)."""
    load_dotenv(dotenv_path or REPO_ROOT / ".env")
    api_key = os.environ.get("ZOTERO_API_KEY")
    user_id = os.environ.get("ZOTERO_USER_ID")
    if not api_key:
        raise ConfigError("ZOTERO_API_KEY is not set — add it to .env at the repo root")
    if not user_id:
        raise ConfigError("ZOTERO_USER_ID is not set — add it to .env at the repo root")
    return Credentials(api_key=api_key, user_id=user_id)


def data_dir() -> Path:
    """Zelador's user data directory: $ZELADOR_DATA_DIR or the platform-native location."""
    override = os.environ.get("ZELADOR_DATA_DIR")
    if override:
        return Path(override)
    return Path(platformdirs.user_data_dir("zelador"))


def ensure_dir(subdir: str) -> Path:
    """Return <data dir>/<subdir>, creating it if needed."""
    path = data_dir() / subdir
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_config(path: Path = CONFIG_FILE) -> Config:
    """Load config.yaml (three keys, all optional); absent file means defaults."""
    if not path.exists():
        return Config()
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must be a YAML mapping")
    allowed = {"zotero_data_dir", "citekey_sources", "style"}
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"unknown config key(s) in {path}: {', '.join(sorted(unknown))}")
    sources = raw.get("citekey_sources") or []
    if not isinstance(sources, list):
        raise ConfigError(f"citekey_sources in {path} must be a list of paths/globs")
    return Config(
        zotero_data_dir=Path(raw["zotero_data_dir"]) if raw.get("zotero_data_dir") else None,
        citekey_sources=[str(s) for s in sources],
        style=raw.get("style") or "apa",
    )


def _is_wsl() -> bool:
    return "microsoft" in platform.uname().release.lower()


def discover_zotero_dir(override: Path | None = None, wsl: bool | None = None) -> Path:
    """Locate the Zotero data directory (config override, else per-platform default)."""
    if override is not None:
        if not override.is_dir():
            raise ConfigError(f"configured zotero_data_dir does not exist: {override}")
        return override
    if wsl is None:
        wsl = _is_wsl()
    if wsl:
        candidates = sorted(
            c for c in WINDOWS_USERS_ROOT.glob("*/Zotero") if (c / "zotero.sqlite").exists()
        )
        if candidates:
            return candidates[0]
        raise ConfigError(f"no Zotero data dir found under {WINDOWS_USERS_ROOT}/*/Zotero")
    home_dir = Path.home() / "Zotero"
    if (home_dir / "zotero.sqlite").exists():
        return home_dir
    raise ConfigError(f"no Zotero data dir found at {home_dir}")
