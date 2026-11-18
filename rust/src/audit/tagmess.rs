//! Audit check 2: tag mess – near/case-duplicate clusters (port of tagmess.py).

use std::collections::BTreeMap;

use serde_json::{Value, json};

use super::{Library, finding};
use crate::pyjson::py_str;

/// `re.sub(r"[^a-z0-9]+", " ", tag.lower()).strip()`
fn normalize(tag: &str) -> String {
    let mut out = String::new();
    for c in tag.to_lowercase().chars() {
        if c.is_ascii_lowercase() || c.is_ascii_digit() {
            out.push(c);
        } else if !out.ends_with(' ') {
            out.push(' ');
        }
    }
    out.trim_matches(' ').to_string()
}

pub fn check(library: &Library) -> Vec<Value> {
    // BTreeMap iterates in code-point order, like sorted() over str keys.
    let mut clusters: BTreeMap<String, Vec<&Value>> = BTreeMap::new();
    for tag in &library.tags {
        clusters
            .entry(normalize(&py_str(&tag["tag"])))
            .or_default()
            .push(tag);
    }
    clusters
        .values()
        .filter(|members| members.len() >= 2)
        .map(|members| {
            let tags: Vec<Value> = members
                .iter()
                .map(|t| {
                    let meta = t.get("meta").cloned().unwrap_or_else(|| json!({}));
                    let kind = if meta.get("type") == Some(&json!(1)) {
                        "auto"
                    } else {
                        "manual"
                    };
                    json!({
                        "tag": t["tag"],
                        "type": kind,
                        "numItems": meta.get("numItems").cloned().unwrap_or(json!(0)),
                    })
                })
                .collect();
            let names: Vec<String> = tags.iter().map(|t| py_str(&t["tag"])).collect();
            finding(
                "tags",
                "near_duplicate_cluster",
                vec![],
                format!("probable duplicates: {}", names.join(", ")),
                json!({"tags": tags}),
            )
        })
        .collect()
}
