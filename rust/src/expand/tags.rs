//! Tag ops: merge_tag, add_tag, remove_tag, and the registry checks every tag
//! write goes through.

use serde_json::{Value, json};

use super::{At, Change, Expander, HIGH, LOW, strings, tag_of, text};
use crate::pyjson::{py_repr, py_str};

fn tag_entry(tag: &str) -> Value {
    json!({"tag": tag, "type": 0})
}

impl Expander<'_, '_> {
    /// True when `tag` may be written. Aliases point at their canonical owner.
    pub(super) fn canonical_tag(&mut self, at: At, tag: &str) -> bool {
        let Some(taxonomy) = self.taxonomy else {
            self.fail(at, "writing tags requires taxonomy.yaml");
            return false;
        };
        if taxonomy.is_canonical(tag) {
            return true;
        }
        let message = match taxonomy.alias_owner(tag) {
            Some(owner) => format!(
                "{} is an alias — the canonical tag is {}",
                py_repr(tag),
                py_repr(owner)
            ),
            None => format!("{} is not a canonical registry tag", py_repr(tag)),
        };
        self.fail(at, message);
        false
    }

    /// The registry checks for a new item's tags: every tag canonical, at most
    /// one tag of each exclusive family. Every failure is reported.
    pub(super) fn check_create_tags(&mut self, at: At, tags: &[String]) -> bool {
        let checked: Vec<bool> = tags.iter().map(|t| self.canonical_tag(at, t)).collect();
        let mut ok = checked.iter().all(|c| *c);
        let Some(taxonomy) = self.taxonomy else {
            return ok;
        };
        for (family, spec) in &taxonomy.families {
            let prefix = format!("{family}:");
            if spec.exclusive && tags.iter().filter(|t| t.starts_with(&prefix)).count() > 1 {
                self.fail(
                    at,
                    format!("would carry multiple '{family}:' tags — family is exclusive"),
                );
                ok = false;
            }
        }
        ok
    }

    pub(super) fn merge_tag(&mut self, at: At, intent: &Value) {
        let (sources, target) = (strings(&intent["from"]), text(intent, "into"));
        if !self.canonical_tag(at, &target) {
            return;
        }
        let taxonomy = self.taxonomy.expect("canonical_tag needs a registry");
        let merged_canonical: Vec<&str> = sources
            .iter()
            .map(String::as_str)
            .filter(|t| taxonomy.is_canonical(t))
            .collect();
        if !merged_canonical.is_empty() {
            let message = format!(
                "cannot merge canonical tag(s) away: {}",
                merged_canonical.join(", ")
            );
            self.fail(at, message);
            return;
        }
        let is_source = |t: &Value| tag_of(t).is_some_and(|name| sources.iter().any(|s| s == name));
        let keys: Vec<String> = self.items.iter().map(|i| py_str(&i["key"])).collect();
        for key in keys {
            let tags = self.item_tags(&key);
            if !tags.iter().any(is_source) {
                continue;
            }
            let mut new: Vec<Value> = tags.iter().filter(|t| !is_source(t)).cloned().collect();
            if !new.iter().any(|t| tag_of(t) == Some(target.as_str())) {
                new.push(tag_entry(&target));
            }
            let change = Change::new("tags", Value::Array(tags), Value::Array(new), HIGH);
            self.apply_item(at, &key, "tags", change);
        }
    }

    pub(super) fn add_tag(&mut self, at: At, intent: &Value) {
        let tag = text(intent, "tag");
        if !self.canonical_tag(at, &tag) {
            return;
        }
        for key in strings(&intent["keys"]) {
            if !self.require_item(at, &key) {
                continue;
            }
            let tags = self.item_tags(&key);
            if tags.iter().any(|t| tag_of(t) == Some(tag.as_str())) {
                continue;
            }
            let mut new = tags.clone();
            new.push(tag_entry(&tag));
            let change = Change::new("tags", Value::Array(tags), Value::Array(new), LOW);
            self.apply_item(at, &key, "tags", change);
        }
    }

    pub(super) fn remove_tag(&mut self, at: At, intent: &Value) {
        let tag = text(intent, "tag");
        for key in strings(&intent["keys"]) {
            if !self.require_item(at, &key) {
                continue;
            }
            let tags = self.item_tags(&key);
            if !tags.iter().any(|t| tag_of(t) == Some(tag.as_str())) {
                continue;
            }
            let new = tags
                .iter()
                .filter(|t| tag_of(t) != Some(tag.as_str()))
                .cloned()
                .collect();
            let change = Change::new("tags", Value::Array(tags), Value::Array(new), HIGH);
            self.apply_item(at, &key, "tags", change);
        }
    }
}
