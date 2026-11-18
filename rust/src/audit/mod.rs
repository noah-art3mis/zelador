//! Audit orchestration (port of audit/runner.py and audit/report.py) for the
//! two checks the spike ports: `tags` and `collections`.

mod hygiene;
mod tagmess;

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};

use chrono::Utc;
use serde_json::{Map, Value, json};

use crate::client::ZoteroClient;
use crate::config::Error;
use crate::pyjson::{dumps_indent, py_str};
use crate::status::{sorted_glob, stem};

pub const PORTED_CHECKS: [&str; 2] = ["tags", "collections"];
const NON_BIBLIOGRAPHIC: [&str; 3] = ["note", "annotation", "attachment"];
const MAX_EXAMPLES: usize = 20;
const CHECK_ORDER: [&str; 6] = [
    "completeness",
    "tags",
    "collections",
    "duplicates",
    "registry",
    "citekeys",
];

pub struct Library {
    pub items: Vec<Value>,
    pub collections: Vec<Value>,
    pub tags: Vec<Value>,
}

impl Library {
    /// Items that belong in a bibliography – no notes, annotations, attachments.
    pub fn regular_items(&self) -> impl Iterator<Item = &Value> {
        self.items.iter().filter(|i| {
            !NON_BIBLIOGRAPHIC.contains(&i["data"]["itemType"].as_str().unwrap_or_default())
        })
    }
}

pub fn finding(
    check: &str,
    kind: &str,
    keys: Vec<String>,
    message: String,
    detail: Value,
) -> Value {
    json!({"check": check, "kind": kind, "keys": keys, "message": message, "detail": detail})
}

/// `datetime.now(UTC).isoformat()` – the fraction is omitted when it is zero.
fn now_iso() -> String {
    let now = Utc::now();
    let base = now.format("%Y-%m-%dT%H:%M:%S");
    match now.timestamp_subsec_micros() {
        0 => format!("{base}+00:00"),
        micros => format!("{base}.{micros:06}+00:00"),
    }
}

/// Run one ported check; write `<check>.json` and regenerate the report.
pub fn run_audit(
    client: &mut ZoteroClient,
    audit_dir: &Path,
    check: &str,
    since: Option<i64>,
    style: &str,
) -> Result<Value, Error> {
    let library = Library {
        items: client.all_items("data,bib", style)?,
        collections: client.all_collections()?,
        tags: client.all_tags()?,
    };
    let version = json!(client.last_modified_version);
    let timestamp = now_iso();

    let mut findings = match check {
        "tags" => tagmess::check(&library),
        "collections" => hygiene::check(&library),
        other => return Err(Error::BadInput(format!("check {other} is not ported"))),
    };
    if let Some(since) = since {
        let scoped: HashSet<&str> = library
            .items
            .iter()
            .chain(&library.collections)
            .filter(|o| o["version"].as_i64().is_some_and(|v| v > since))
            .filter_map(|o| o["key"].as_str())
            .collect();
        findings.retain(|f| {
            f["keys"]
                .as_array()
                .into_iter()
                .flatten()
                .any(|k| k.as_str().is_some_and(|k| scoped.contains(k)))
        });
    }
    let count = findings.len();
    let payload = json!({
        "check": check,
        "library_version": version,
        "timestamp": timestamp,
        "since": since,
        "findings": findings,
    });
    fs::write(
        audit_dir.join(format!("{check}.json")),
        dumps_indent(&payload) + "\n",
    )?;

    let report = write_report(audit_dir)?;
    let mut counts = Map::new();
    counts.insert(check.into(), json!(count));
    Ok(json!({
        "library_version": version,
        "timestamp": timestamp,
        "counts": counts,
        "report": report.to_string_lossy(),
    }))
}

/// Regenerate audit-report.md from every check JSON on disk.
fn write_report(audit_dir: &Path) -> Result<PathBuf, Error> {
    let mut paths = sorted_glob(audit_dir, ".json")?;
    let rank = |p: &Path| {
        let name = stem(p);
        let index = CHECK_ORDER
            .iter()
            .position(|c| *c == name)
            .unwrap_or(CHECK_ORDER.len());
        (index, name)
    };
    paths.sort_by_key(|p| rank(p));

    let mut lines: Vec<String> = vec!["# Audit report".into(), String::new()];
    for path in &paths {
        let payload: Value = serde_json::from_str(&fs::read_to_string(path)?)
            .map_err(|e| Error::Config(format!("{}: {e}", path.display())))?;
        let findings = payload["findings"]
            .as_array()
            .ok_or_else(|| Error::Config(format!("{}: no findings list", path.display())))?;
        let since = &payload["since"];
        let scoped = !(since.is_null() || since == &json!(0));
        let scope = if scoped {
            format!(", scoped to changes since version {}", py_str(since))
        } else {
            String::new()
        };
        lines.push(format!(
            "## {} — {} finding(s)",
            py_str(&payload["check"]),
            findings.len()
        ));
        lines.push(String::new());
        lines.push(format!(
            "Computed at library version {}, {}{scope}.",
            py_str(&payload["library_version"]),
            py_str(&payload["timestamp"])
        ));
        lines.push(String::new());
        if findings.is_empty() {
            lines.push("Nothing to report.".into());
            lines.push(String::new());
            continue;
        }
        // Counter.most_common: count descending, ties in first-seen order.
        let mut by_kind: Vec<(String, usize)> = Vec::new();
        for f in findings {
            let kind = py_str(&f["kind"]);
            match by_kind.iter_mut().find(|(k, _)| *k == kind) {
                Some(entry) => entry.1 += 1,
                None => by_kind.push((kind, 1)),
            }
        }
        by_kind.sort_by_key(|entry| std::cmp::Reverse(entry.1));
        lines.extend(by_kind.iter().map(|(kind, n)| format!("- **{kind}**: {n}")));
        lines.push(String::new());
        for f in findings.iter().take(MAX_EXAMPLES) {
            let keys: Vec<String> = f["keys"]
                .as_array()
                .into_iter()
                .flatten()
                .map(py_str)
                .collect();
            let keys = if keys.is_empty() {
                "—".to_string()
            } else {
                keys.join(", ")
            };
            lines.push(format!("- `{keys}` {}", py_str(&f["message"])));
        }
        if findings.len() > MAX_EXAMPLES {
            let name = path.file_name().unwrap_or_default().to_string_lossy();
            lines.push(format!(
                "- … and {} more (see {name})",
                findings.len() - MAX_EXAMPLES
            ));
        }
        lines.push(String::new());
    }
    let path = audit_dir.join("audit-report.md");
    fs::write(&path, lines.join("\n"))?;
    Ok(path)
}
