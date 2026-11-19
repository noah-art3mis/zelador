//! Collection membership (add_to_collection, remove_from_collection) and
//! collection objects (create, rename, move, trash).

use std::collections::HashSet;

use serde_json::{Value, json};

use super::{At, Change, Expander, HIGH, LOW, strings, text};
use crate::pyjson::{py_eq, py_str, py_truthy};

impl Expander<'_, '_> {
    // -- collection membership ------------------------------------------------

    pub(super) fn membership(&mut self, at: At, intent: &Value, add: bool) {
        let ckey = text(intent, "collection");
        if !self.colls_by_key.contains_key(&ckey) {
            self.fail(at, format!("no such collection: {ckey}"));
            return;
        }
        let member = json!(ckey);
        for key in strings(&intent["keys"]) {
            if !self.require_item(at, &key) {
                continue;
            }
            let memberships = self
                .item_state(&key)
                .get("collections")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            if add == memberships.iter().any(|m| py_eq(m, &member)) {
                continue;
            }
            let (new, risk): (Vec<Value>, _) = if add {
                let new = memberships.iter().cloned().chain([member.clone()]);
                (new.collect(), LOW)
            } else {
                let new = memberships.iter().filter(|m| !py_eq(m, &member)).cloned();
                (new.collect(), HIGH)
            };
            let change = Change::new(
                "collections",
                Value::Array(memberships),
                Value::Array(new),
                risk,
            );
            self.apply_item(at, &key, "collections", change);
        }
    }

    // -- collection objects ---------------------------------------------------

    pub(super) fn create_collection(&mut self, at: At, intent: &Value) {
        let parent = intent["parent"].as_str().map(String::from);
        if let Some(parent) = &parent
            && !self.colls_by_key.contains_key(parent)
        {
            self.fail(at, format!("no such parent collection: {parent}"));
            return;
        }
        let new = json!({
            "name": intent["name"],
            "parentCollection": parent.map_or(json!(false), Value::String),
        });
        self.create(at, "collection", new);
    }

    pub(super) fn rename_collection(&mut self, at: At, intent: &Value) {
        let key = text(intent, "collection");
        if !self.require_collection(at, &key) {
            return;
        }
        let old = self
            .coll_state(&key)
            .get("name")
            .cloned()
            .unwrap_or_default();
        if py_eq(&old, &intent["name"]) {
            return;
        }
        let change = Change::new("name", old, intent["name"].clone(), LOW);
        self.apply_coll(at, &key, "name", change);
    }

    pub(super) fn move_collection(&mut self, at: At, intent: &Value) {
        let key = text(intent, "collection");
        if !self.require_collection(at, &key) {
            return;
        }
        let parent = intent["parent"].as_str().map(String::from);
        if let Some(parent) = &parent {
            if !self.colls_by_key.contains_key(parent) {
                self.fail(at, format!("no such parent collection: {parent}"));
                return;
            }
            if self.is_descendant(parent, &key) {
                let message = format!("{parent} is a descendant of {key} — the move would cycle");
                self.fail(at, message);
                return;
            }
        }
        let new = parent.map_or(json!(false), Value::String);
        let old = self
            .coll_state(&key)
            .get("parentCollection")
            .cloned()
            .unwrap_or_default();
        if py_eq(&old, &new) {
            return;
        }
        let change = Change::new("parentCollection", old, new, HIGH);
        self.apply_coll(at, &key, "parentCollection", change);
    }

    pub(super) fn trash_collection(&mut self, at: At, intent: &Value) {
        let key = text(intent, "collection");
        if !self.require_collection(at, &key)
            || self.coll_state(&key).get("deleted").is_some_and(py_truthy)
        {
            return;
        }
        let change = Change::new("deleted", json!(false), json!(true), HIGH);
        self.apply_coll(at, &key, "deleted", change);
    }

    /// Walk parent links in working state; `of` reachable from `key` means
    /// descendant. A parent missing from the library ends the walk (Python
    /// raises KeyError there).
    fn is_descendant(&self, key: &str, of: &str) -> bool {
        let mut seen = HashSet::new();
        let mut current = Some(key.to_string());
        while let Some(at) = current.take() {
            if !seen.insert(at.clone()) {
                break;
            }
            if at == of {
                return true;
            }
            let parent = match self.working_colls.get(&at) {
                Some(working) if !working.is_empty() => working.get("parentCollection"),
                _ => self
                    .colls_by_key
                    .get(&at)
                    .and_then(|c| c["data"].get("parentCollection")),
            };
            current = parent.filter(|p| py_truthy(p)).map(py_str);
        }
        false
    }
}
