"""Session orientation: the local half of `zel status` (backup, logs, audit, config)."""

from __future__ import annotations

import json
from pathlib import Path

from zelador import backup
from zelador.config import CONFIG_FILE, TAXONOMY_FILE, Config
from zelador.write.changelog import is_session_log, unresolved_ops


def classify_logs(log_dir: Path) -> tuple[list[str], list[str]]:
    """Split log/ into (sessions holding unresolved `pending` entries, files that are not logs).

    One pass, so the two lists cannot drift apart. `pending_sessions` is a safety
    gate — apply refuses while it is non-empty — so anything unclassifiable is
    named in the second list rather than dropped.
    """
    pending, foreign = [], []
    for path in sorted(log_dir.glob("*.jsonl")):
        if not is_session_log(path):
            foreign.append(path.stem)
        elif unresolved_ops(path):
            pending.append(path.stem)
    return pending, foreign


def pending_sessions(log_dir: Path) -> list[str]:
    """Session logs holding unresolved `pending` entries — apply refuses while these exist."""
    return classify_logs(log_dir)[0]


def latest_audit(audit_dir: Path) -> dict | None:
    """Newest audit stamp: report presence plus the freshest check's version/timestamp."""
    stamps = []
    for path in audit_dir.glob("*.json"):
        try:
            check = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if "library_version" in check and "timestamp" in check:
            stamps.append((check["timestamp"], check["library_version"]))
    if not stamps:
        return None
    timestamp, version = max(stamps)
    return {
        "timestamp": timestamp,
        "library_version": version,
        "report": (audit_dir / "audit-report.md").exists(),
    }


def local_status(backups_dir: Path, log_dir: Path, audit_dir: Path, cfg: Config) -> dict:
    """Everything `zel status` can say without touching the API."""
    info = backup.latest_backup(backups_dir)
    backup_part = None
    if info is not None:
        stats = backup.backup_stats(info.path)
        backup_part = {
            "timestamp": info.timestamp,
            "library_version": info.library_version,
            "items": stats.items,
            "collections": stats.collections,
            "tags": stats.tags,
        }
    pending, foreign = classify_logs(log_dir)
    return {
        "backup": backup_part,
        "pending_sessions": pending,
        "foreign_logs": foreign,
        "audit": latest_audit(audit_dir),
        "config": {
            "config_yaml": CONFIG_FILE.exists(),
            "taxonomy_yaml": TAXONOMY_FILE.exists(),
            "citekey_sources": bool(cfg.citekey_sources),
        },
    }


def render_status(status: dict) -> list[str]:
    """One-screen human rendering of the assembled status object."""
    api = status["api"]
    if api.get("error"):
        library_line = f"library:   unreachable — {api['error']}"
    else:
        library_line = f"library:   version {api['library_version']} (live)"
    b = status["backup"]
    backup_line = (
        f"backup:    {b['timestamp']} @ version {b['library_version']} — "
        f"{b['items']} items, {b['collections']} collections, {b['tags']} tags"
        if b
        else "backup:    none"
    )
    a = status["audit"]
    audit_line = (
        f"audit:     {a['timestamp']} @ version {a['library_version']}"
        f"{'' if a['report'] else ' (report missing)'}"
        if a
        else "audit:     none"
    )
    pending = status["pending_sessions"]
    pending_line = f"pending:   {', '.join(pending) if pending else 'none'}"
    cfg = status["config"]
    config_line = (
        f"config:    config.yaml {'yes' if cfg['config_yaml'] else 'no'} · "
        f"taxonomy.yaml {'yes' if cfg['taxonomy_yaml'] else 'no'} · "
        f"citekey_sources {'yes' if cfg['citekey_sources'] else 'no'}"
    )
    lines = [library_line, backup_line, audit_line, pending_line, config_line]
    foreign = status["foreign_logs"]
    if foreign:
        lines.append(
            f"log/:      {len(foreign)} file(s) that are not session logs: {', '.join(foreign)}"
        )
    return lines
