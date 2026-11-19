//! Session orientation – the local half of `zel status` (port of status.py,
//! plus the read-only parts of backup.py and write/changelog.py it needs).

use std::collections::VecDeque;
use std::fs;
use std::path::{Path, PathBuf};

use indexmap::IndexMap;
use serde_json::{Map, Value, json};

use crate::backup::parse_line;
use crate::config::{Config, Error, config_file, taxonomy_file};
use crate::pyjson::py_str;

const LOG_SCHEMA: &str = "log.v1";

/// `sorted(dir.glob("*<suffix>"))` – pathlib's glob also matches dotfiles.
pub fn sorted_glob(dir: &Path, suffix: &str) -> Result<Vec<PathBuf>, Error> {
    let mut paths: Vec<PathBuf> = fs::read_dir(dir)?
        .filter_map(|entry| entry.ok().map(|e| e.path()))
        .filter(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|n| n.ends_with(suffix))
        })
        .collect();
    paths.sort();
    Ok(paths)
}

pub fn stem(path: &Path) -> String {
    path.file_stem()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_default()
}

// -- backups -------------------------------------------------------------------

fn backup_part(backups_dir: &Path) -> Result<Value, Error> {
    let Some(info) = crate::backup::latest_backup(backups_dir)? else {
        return Ok(Value::Null);
    };
    let stats = crate::backup::backup_stats(&info.path)?;
    Ok(json!({
        "timestamp": info.timestamp,
        "library_version": info.library_version,
        "items": stats.items,
        "collections": stats.collections,
        "tags": stats.tags,
    }))
}

// -- session logs --------------------------------------------------------------

/// The first non-blank line, if it declares a log.v1 header.
fn is_session_log(path: &Path) -> bool {
    let Ok(text) = fs::read_to_string(path) else {
        return false;
    };
    let Some(first) = text.lines().find(|l| !l.trim().is_empty()) else {
        return false;
    };
    serde_json::from_str::<Value>(first).is_ok_and(|line| is_header(&line))
}

fn is_header(line: &Value) -> bool {
    line.get("kind").and_then(Value::as_str) == Some("header")
        && line.get("schema").and_then(Value::as_str) == Some(LOG_SCHEMA)
}

/// Whether any operation's last status is still `pending` (a crashed apply).
fn has_unresolved_ops(path: &Path) -> Result<bool, Error> {
    let text = fs::read_to_string(path)?;
    let mut status: IndexMap<String, String> = IndexMap::new();
    let mut lines: VecDeque<&str> = text.lines().filter(|l| !l.trim().is_empty()).collect();
    lines.pop_front(); // the header, already validated by is_session_log
    for raw in lines {
        let line = parse_line(path, raw)?;
        if line["kind"] == "header" {
            continue;
        }
        let (Some(op), Some(state)) = (line["op"].as_str(), line["status"].as_str()) else {
            return Err(Error::Config(format!(
                "{}: entry without op/status",
                path.display()
            )));
        };
        status.insert(op.to_string(), state.to_string());
    }
    Ok(status.values().any(|s| s == "pending"))
}

/// Split log/ into (sessions with unresolved entries, files that are not logs).
fn classify_logs(log_dir: &Path) -> Result<(Vec<String>, Vec<String>), Error> {
    let (mut pending, mut foreign) = (Vec::new(), Vec::new());
    for path in sorted_glob(log_dir, ".jsonl")? {
        if !is_session_log(&path) {
            foreign.push(stem(&path));
        } else if has_unresolved_ops(&path)? {
            pending.push(stem(&path));
        }
    }
    Ok((pending, foreign))
}

// -- audit stamp ---------------------------------------------------------------

fn latest_audit(audit_dir: &Path) -> Result<Value, Error> {
    let mut best: Option<(String, Value)> = None;
    for path in sorted_glob(audit_dir, ".json")? {
        let Ok(check) = serde_json::from_str::<Value>(&fs::read_to_string(&path)?) else {
            continue;
        };
        let (Some(ts), Some(version)) = (check.get("timestamp"), check.get("library_version"))
        else {
            continue;
        };
        let candidate = (py_str(ts), version.clone());
        let newer = match &best {
            None => true,
            Some((best_ts, best_v)) => {
                (&candidate.0, candidate.1.as_i64()) > (best_ts, best_v.as_i64())
            }
        };
        if newer {
            best = Some(candidate);
        }
    }
    Ok(match best {
        None => Value::Null,
        Some((timestamp, version)) => json!({
            "timestamp": timestamp,
            "library_version": version,
            "report": audit_dir.join("audit-report.md").exists(),
        }),
    })
}

/// Everything `zel status` can say without touching the API.
pub fn local_status(
    backups_dir: &Path,
    log_dir: &Path,
    audit_dir: &Path,
    cfg: &Config,
) -> Result<Map<String, Value>, Error> {
    let (pending, foreign) = classify_logs(log_dir)?;
    let mut status = Map::new();
    status.insert("backup".into(), backup_part(backups_dir)?);
    status.insert("pending_sessions".into(), json!(pending));
    status.insert("foreign_logs".into(), json!(foreign));
    status.insert("audit".into(), latest_audit(audit_dir)?);
    status.insert(
        "config".into(),
        json!({
            "config_yaml": config_file().exists(),
            "taxonomy_yaml": taxonomy_file().exists(),
            "citekey_sources": !cfg.citekey_sources.is_empty(),
        }),
    );
    Ok(status)
}

fn yes_no(value: &Value) -> &'static str {
    if value.as_bool() == Some(true) {
        "yes"
    } else {
        "no"
    }
}

/// One-screen human rendering of the assembled status object.
pub fn render_status(status: &Map<String, Value>) -> Vec<String> {
    let api = &status["api"];
    let library_line = if !api["error"].is_null() {
        format!("library:   unreachable — {}", py_str(&api["error"]))
    } else {
        format!(
            "library:   version {} (live)",
            py_str(&api["library_version"])
        )
    };
    let b = &status["backup"];
    let backup_line = if b.is_null() {
        "backup:    none".to_string()
    } else {
        format!(
            "backup:    {} @ version {} — {} items, {} collections, {} tags",
            py_str(&b["timestamp"]),
            py_str(&b["library_version"]),
            b["items"],
            b["collections"],
            b["tags"]
        )
    };
    let a = &status["audit"];
    let audit_line = if a.is_null() {
        "audit:     none".to_string()
    } else {
        let missing = if a["report"].as_bool() == Some(true) {
            ""
        } else {
            " (report missing)"
        };
        format!(
            "audit:     {} @ version {}{missing}",
            py_str(&a["timestamp"]),
            py_str(&a["library_version"])
        )
    };
    let names = |key: &str| -> Vec<String> {
        status[key]
            .as_array()
            .into_iter()
            .flatten()
            .map(py_str)
            .collect()
    };
    let pending = names("pending_sessions");
    let pending_line = if pending.is_empty() {
        "pending:   none".to_string()
    } else {
        format!("pending:   {}", pending.join(", "))
    };
    let cfg = &status["config"];
    let config_line = format!(
        "config:    config.yaml {} · taxonomy.yaml {} · citekey_sources {}",
        yes_no(&cfg["config_yaml"]),
        yes_no(&cfg["taxonomy_yaml"]),
        yes_no(&cfg["citekey_sources"])
    );
    let mut lines = vec![
        library_line,
        backup_line,
        audit_line,
        pending_line,
        config_line,
    ];
    let foreign = names("foreign_logs");
    if !foreign.is_empty() {
        lines.push(format!(
            "log/:      {} file(s) that are not session logs: {}",
            foreign.len(),
            foreign.join(", ")
        ));
    }
    lines
}
