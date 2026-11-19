//! The changeset.v1 and plan.v1 contracts (port of write/contracts.py).
//!
//! Changesets are symbolic intents in a closed operation vocabulary; plans are
//! their expansion into exact version-pinned per-object operations. Both are
//! linted structurally here – semantic validation against the live library and
//! the taxonomy happens in expand.

use std::fs;
use std::path::{Path, PathBuf};

use chrono::{DateTime, Utc};
use serde_json::{Map, Value, json};

use crate::config::{Error, normalise_dir};
use crate::jsondecode;
use crate::pyjson::{dumps_indent, py_repr, py_repr_value, re_dollar_body};

/// Closed operation vocabulary: op -> (field, type spec), in Python's order. A
/// trailing "?" marks an optional field; specs are "str", "str_list"
/// (non-empty), "str_or_null", "str_map" (non-empty, str -> non-empty str),
/// "creator_list" (Zotero creator objects). Deletes are unrepresentable by
/// construction – trash_* is the only removal.
pub const OPS: &[(&str, &[(&str, &str)])] = &[
    ("merge_tag", &[("from", "str_list"), ("into", "str")]),
    ("add_tag", &[("tag", "str"), ("keys", "str_list")]),
    ("remove_tag", &[("tag", "str"), ("keys", "str_list")]),
    (
        "fill_field",
        &[("key", "str"), ("field", "str"), ("value", "str")],
    ),
    ("clear_field", &[("key", "str"), ("field", "str")]),
    (
        "set_creators",
        &[("key", "str"), ("creators", "creator_list")],
    ),
    ("set_item_type", &[("key", "str"), ("itemType", "str")]),
    (
        "create_item",
        &[
            ("itemType", "str"),
            ("fields", "str_map"),
            ("creators", "creator_list?"),
            ("tags", "str_list?"),
            ("collections", "str_list?"),
            ("attachment", "str?"),
        ],
    ),
    (
        "add_to_collection",
        &[("collection", "str"), ("keys", "str_list")],
    ),
    (
        "remove_from_collection",
        &[("collection", "str"), ("keys", "str_list")],
    ),
    (
        "create_collection",
        &[("name", "str"), ("parent", "str_or_null?")],
    ),
    (
        "rename_collection",
        &[("collection", "str"), ("name", "str")],
    ),
    (
        "move_collection",
        &[("collection", "str"), ("parent", "str_or_null")],
    ),
    ("trash_collection", &[("collection", "str")]),
    ("trash_item", &[("key", "str")]),
];

pub struct Changeset {
    pub slug: String,
    pub intents: Vec<Value>,
}

/// One expanded write facet of one intent group, pinned to an object version.
pub struct Operation {
    pub id: String,
    pub group: usize,
    pub op: String,
    pub kind: &'static str,
    pub key: String,
    pub version: Value,
    pub facet: String,
    pub old: Value,
    pub new: Value,
    pub risk: &'static str,
}

pub struct Plan {
    pub id: String,
    pub slug: String,
    pub backup: Value,
    pub library_version: i64,
    pub intents: Vec<Value>,
    pub operations: Vec<Operation>,
    pub settings: Option<Value>,
}

impl Plan {
    /// `{"schema": "plan.v1", **asdict(plan)}`, in the dataclass field order.
    pub fn to_json(&self) -> Value {
        let operations: Vec<Value> = self
            .operations
            .iter()
            .map(|op| {
                json!({
                    "id": op.id, "group": op.group, "op": op.op, "kind": op.kind,
                    "key": op.key, "version": op.version, "facet": op.facet,
                    "old": op.old, "new": op.new, "risk": op.risk,
                })
            })
            .collect();
        json!({
            "schema": "plan.v1",
            "id": self.id,
            "slug": self.slug,
            "backup": self.backup,
            "library_version": self.library_version,
            "intents": self.intents,
            "operations": operations,
            "settings": self.settings,
        })
    }
}

fn bad(message: String) -> Error {
    Error::BadInput(message)
}

/// Parse and structurally lint a changeset.v1 file. `reference` is the
/// command-line argument, used as given like Python's `Path(changeset)`.
pub fn load_changeset(reference: &str) -> Result<Changeset, Error> {
    let path = normalise_dir(reference);
    let shown = path.display();
    let raw = read_json(&path)?;
    let schema = raw.get("schema").unwrap_or(&Value::Null);
    if schema != "changeset.v1" {
        return Err(bad(format!(
            "{shown}: schema must be 'changeset.v1', got {}",
            py_repr_value(schema)
        )));
    }
    let slug = raw.get("slug").unwrap_or(&Value::Null);
    let Some(slug) = slug.as_str().filter(|s| is_slug(s)) else {
        return Err(bad(format!(
            "{shown}: slug must be lowercase kebab-case, got {}",
            py_repr_value(slug)
        )));
    };
    let intents = match raw.get("intents") {
        Some(Value::Array(list)) if !list.is_empty() => list.clone(),
        _ => return Err(bad(format!("{shown}: intents must be a non-empty list"))),
    };
    for (index, intent) in intents.iter().enumerate() {
        lint_intent(intent, &format!("{shown}: intent {index}"))?;
    }
    Ok(Changeset {
        slug: slug.to_string(),
        intents,
    })
}

/// `^[a-z0-9][a-z0-9-]*$` under `re.match`.
fn is_slug(s: &str) -> bool {
    let mut chars = re_dollar_body(s).chars();
    chars
        .next()
        .is_some_and(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
}

fn lint_intent(intent: &Value, place: &str) -> Result<(), Error> {
    let Some(map) = intent.as_object() else {
        return Err(bad(format!(
            "{place}: must be a mapping, got {}",
            py_repr_value(intent)
        )));
    };
    let op_value = map.get("op").unwrap_or(&Value::Null);
    let Some((op, spec)) = OPS.iter().find(|(name, _)| op_value.as_str() == Some(name)) else {
        let mut allowed: Vec<&str> = OPS.iter().map(|(name, _)| *name).collect();
        allowed.sort();
        return Err(bad(format!(
            "{place}: unknown op {} — allowed: {}",
            py_repr_value(op_value),
            allowed.join(", ")
        )));
    };
    let mut unknown: Vec<&str> = map
        .keys()
        .map(String::as_str)
        .filter(|k| *k != "op" && !spec.iter().any(|(name, _)| name == k))
        .collect();
    if !unknown.is_empty() {
        unknown.sort();
        return Err(bad(format!(
            "{place}: unknown field(s) on {op}: {}",
            unknown.join(", ")
        )));
    }
    for (name, kind) in spec.iter() {
        let (kind, optional) = match kind.strip_suffix('?') {
            Some(kind) => (kind, true),
            None => (*kind, false),
        };
        match map.get(*name) {
            None if optional => {}
            None => {
                return Err(bad(format!(
                    "{place}: {op} requires field {}",
                    py_repr(name)
                )));
            }
            Some(value) => check_type(value, kind, &format!("{place}: {op}.{name}"))?,
        }
    }
    Ok(())
}

fn non_empty_str(value: &Value) -> bool {
    value.as_str().is_some_and(|s| !s.is_empty())
}

fn check_type(value: &Value, kind: &str, place: &str) -> Result<(), Error> {
    let got = py_repr_value(value);
    match kind {
        "str" if !non_empty_str(value) => Err(bad(format!(
            "{place} must be a non-empty string, got {got}"
        ))),
        "str_or_null" if !value.is_null() && !non_empty_str(value) => Err(bad(format!(
            "{place} must be a non-empty string or null, got {got}"
        ))),
        "str_list"
            if !value
                .as_array()
                .is_some_and(|l| !l.is_empty() && l.iter().all(non_empty_str)) =>
        {
            Err(bad(format!(
                "{place} must be a non-empty list of strings, got {got}"
            )))
        }
        "str_map"
            if !value.as_object().is_some_and(|m| {
                !m.is_empty() && m.iter().all(|(k, v)| !k.is_empty() && non_empty_str(v))
            }) =>
        {
            Err(bad(format!(
                "{place} must be a non-empty mapping of non-empty strings, got {got}"
            )))
        }
        "creator_list" => match value.as_array() {
            Some(list) if !list.is_empty() => list
                .iter()
                .try_for_each(|creator| check_creator(creator, place)),
            _ => Err(bad(format!(
                "{place} must be a non-empty list of creators, got {got}"
            ))),
        },
        _ => Ok(()),
    }
}

/// One Zotero creator: creatorType plus either a single `name` or
/// firstName/lastName – never both forms, never other keys.
fn check_creator(creator: &Value, place: &str) -> Result<(), Error> {
    let Some(map) = creator.as_object() else {
        return Err(bad(format!(
            "{place}: creator must be a mapping, got {}",
            py_repr_value(creator)
        )));
    };
    let allowed = ["creatorType", "name", "firstName", "lastName"];
    let mut unknown: Vec<&str> = map
        .keys()
        .map(String::as_str)
        .filter(|k| !allowed.contains(k))
        .collect();
    if !unknown.is_empty() {
        unknown.sort();
        return Err(bad(format!(
            "{place}: unknown creator key(s): {}",
            unknown.join(", ")
        )));
    }
    if !map.get("creatorType").is_some_and(non_empty_str) {
        return Err(bad(format!(
            "{place}: creator requires a non-empty creatorType"
        )));
    }
    let has_name = map.contains_key("name");
    let has_parts = map.contains_key("firstName") || map.contains_key("lastName");
    if has_name == has_parts {
        return Err(bad(format!(
            "{place}: creator takes either name or firstName/lastName, got {}",
            py_repr_value(creator)
        )));
    }
    for field in ["name", "firstName", "lastName"] {
        if map.get(field).is_some_and(|v| !non_empty_str(v)) {
            return Err(bad(format!(
                "{place}: creator {field} must be a non-empty string"
            )));
        }
    }
    Ok(())
}

pub fn plan_id(slug: &str, now: DateTime<Utc>) -> String {
    format!("{}-{slug}", now.format("%Y%m%dT%H%M%SZ"))
}

pub fn save_plan(plan: &Plan, plans_dir: &Path) -> Result<PathBuf, Error> {
    let path = plans_dir.join(format!("{}.json", plan.id));
    fs::write(&path, dumps_indent(&plan.to_json()) + "\n")?;
    Ok(path)
}

/// `json.loads(path.read_text())`, which reads with universal newlines.
fn read_json(path: &Path) -> Result<Map<String, Value>, Error> {
    let shown = path.display();
    let text = match fs::read_to_string(path) {
        Ok(text) => text.replace("\r\n", "\n").replace('\r', "\n"),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Err(bad(format!("no such file: {shown}")));
        }
        Err(e) => return Err(Error::Config(format!("{shown}: {e}"))),
    };
    match jsondecode::loads(&text) {
        Ok(Value::Object(map)) => Ok(map),
        Ok(_) => Err(bad(format!("{shown} must hold a JSON object"))),
        Err(e) => Err(bad(format!("{shown} is not valid JSON: {e}"))),
    }
}
