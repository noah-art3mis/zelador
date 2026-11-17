//! Audit check 3: collection hygiene – unfiled items, empty/duplicate/orphaned
//! collections (port of hygiene.py).

use std::collections::{BTreeMap, HashMap};

use indexmap::IndexMap;
use serde_json::{Value, json};

use super::{Library, finding};
use crate::pyjson::{py_repr, py_str};

/// `data.get("parentCollection") or False` – Zotero writes `false` for top level.
fn parent_of(data: &Value) -> Option<&str> {
    data.get("parentCollection")
        .and_then(Value::as_str)
        .filter(|p| !p.is_empty())
}

fn name_of(data: &Value) -> String {
    py_str(&data["name"])
}

pub fn check(library: &Library) -> Vec<Value> {
    let mut findings = Vec::new();
    let by_key: IndexMap<&str, &Value> = library
        .collections
        .iter()
        .map(|c| (c["key"].as_str().unwrap_or_default(), &c["data"]))
        .collect();

    let mut membership: HashMap<&str, usize> = HashMap::new();
    for item in library.regular_items() {
        let member_of: Vec<&str> = item["data"]["collections"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .collect();
        for key in &member_of {
            *membership.entry(key).or_default() += 1;
        }
        if member_of.is_empty() {
            let title = item["data"].get("title").map(py_str).unwrap_or_default();
            findings.push(finding(
                "collections",
                "unfiled_item",
                vec![py_str(&item["key"])],
                format!("in no collection: {title}"),
                json!({}),
            ));
        }
    }

    // Keyed by parent, None standing in for Python's `False` (top level).
    let mut children: IndexMap<Option<&str>, Vec<&str>> = IndexMap::new();
    for (key, data) in &by_key {
        children.entry(parent_of(data)).or_default().push(key);
    }

    let mut sorted_keys: Vec<&&str> = by_key.keys().collect();
    sorted_keys.sort();
    for key in sorted_keys {
        let data = by_key[*key];
        let has_children = children.get(&Some(*key)).is_some_and(|c| !c.is_empty());
        if membership.get(*key).copied().unwrap_or(0) == 0 && !has_children {
            findings.push(finding(
                "collections",
                "empty_collection",
                vec![key.to_string()],
                format!("empty collection: {}", name_of(data)),
                json!({}),
            ));
        }
        if let Some(parent) = parent_of(data).filter(|p| !by_key.contains_key(p)) {
            findings.push(finding(
                "collections",
                "orphaned_subtree",
                vec![key.to_string()],
                format!("parent {parent} does not exist: {}", name_of(data)),
                json!({}),
            ));
        }
    }

    // sorted(children.items(), key=lambda pair: str(pair[0])): False sorts as "False".
    let mut groups: Vec<(&Option<&str>, &Vec<&str>)> = children.iter().collect();
    groups.sort_by_key(|(parent, _)| parent.unwrap_or("False"));
    for (_, siblings) in groups {
        let mut names: BTreeMap<String, Vec<String>> = BTreeMap::new();
        for key in siblings {
            let folded = caseless::default_case_fold_str(&name_of(by_key[*key]));
            names.entry(folded).or_default().push(key.to_string());
        }
        for (name, mut keys) in names {
            if keys.len() > 1 {
                keys.sort();
                findings.push(finding(
                    "collections",
                    "duplicate_siblings",
                    keys,
                    format!("duplicate sibling collections named {}", py_repr(&name)),
                    json!({}),
                ));
            }
        }
    }
    findings
}
