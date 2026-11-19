//! Item ops: fill_field, clear_field, set_creators, create_item,
//! set_item_type, trash_item.

use std::collections::HashSet;

use serde_json::{Map, Value, json};

use super::{At, Change, Expander, HIGH, LOW, strings, text};
use crate::config::Error;
use crate::pyjson::{py_eq, py_repr, py_repr_value, py_str, py_truthy};

const EXTRA: &str = "it is plugin territory (Better BibTeX pins citekeys there)";

/// `list(dict.fromkeys(values))`: first occurrence wins, order kept.
fn dedupe(values: impl Iterator<Item = Value>) -> Vec<Value> {
    let mut out: Vec<Value> = Vec::new();
    for value in values {
        if !out.iter().any(|seen| py_eq(seen, &value)) {
            out.push(value);
        }
    }
    out
}

fn list(data: &Map<String, Value>, field: &str) -> Vec<Value> {
    data.get(field)
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
}

impl Expander<'_, '_> {
    /// The valid fields of an item type, or a recorded failure when the
    /// server does not know the type.
    fn fields_of(&mut self, at: At, item_type: &str) -> Result<Option<HashSet<String>>, Error> {
        match self.client.item_type_fields(item_type) {
            Ok(valid) => Ok(Some(valid)),
            Err(Error::Zotero(_)) => {
                self.fail(
                    at,
                    format!("{} is not a valid item type", py_repr(item_type)),
                );
                Ok(None)
            }
            Err(other) => Err(other),
        }
    }

    /// fill_field, and clear_field with `value` empty.
    pub(super) fn write_field(&mut self, at: At, intent: &Value, value: &str) -> Result<(), Error> {
        let (key, field) = (text(intent, "key"), text(intent, "field"));
        if !self.require_item(at, &key) {
            return Ok(());
        }
        if field == "extra" {
            let message = format!("'extra' is never a {} target — {EXTRA}", at.op);
            self.fail(at, message);
            return Ok(());
        }
        let item_type = self.item_state(&key)["itemType"].clone();
        let valid = self.client.item_type_fields(&py_str(&item_type))?;
        if !valid.contains(&field) {
            let message = format!(
                "{} is not a field of {}",
                py_repr(&field),
                py_repr_value(&item_type)
            );
            self.fail(at, message);
            return Ok(());
        }
        let old = match self.item_state(&key).get(&field) {
            Some(old) if py_truthy(old) => old.clone(),
            _ => json!(""),
        };
        let value = json!(value);
        if py_eq(&old, &value) {
            return Ok(());
        }
        let risk = if py_truthy(&old) { HIGH } else { LOW };
        let change = Change::new(format!("field:{field}"), old, value, risk);
        self.apply_item(at, &key, &field, change);
        Ok(())
    }

    pub(super) fn set_creators(&mut self, at: At, intent: &Value) -> Result<(), Error> {
        let (key, creators) = (text(intent, "key"), intent["creators"].clone());
        if !self.require_item(at, &key) {
            return Ok(());
        }
        let item_type = self.item_state(&key)["itemType"].clone();
        let valid = self.client.item_type_creator_types(&py_str(&item_type))?;
        for creator in creators.as_array().into_iter().flatten() {
            let creator_type = py_str(&creator["creatorType"]);
            if !valid.contains(&creator_type) {
                let message = format!(
                    "{} is not a creator type of {}",
                    py_repr(&creator_type),
                    py_repr_value(&item_type)
                );
                self.fail(at, message);
                return Ok(());
            }
        }
        let old = self
            .item_state(&key)
            .get("creators")
            .cloned()
            .unwrap_or(json!([]));
        if py_eq(&old, &creators) {
            return Ok(());
        }
        let risk = if py_truthy(&old) { HIGH } else { LOW };
        self.apply_item(
            at,
            &key,
            "creators",
            Change::new("creators", old, creators, risk),
        );
        Ok(())
    }

    pub(super) fn create_item(&mut self, at: At, intent: &Value) -> Result<(), Error> {
        let item_type = text(intent, "itemType");
        let Some(valid) = self.fields_of(at, &item_type)? else {
            return Ok(());
        };
        let mut ok = self.check_create_fields(at, &item_type, &valid, intent)?;
        let declared_tags = strings(&intent["tags"]);
        ok = self.check_create_tags(at, &declared_tags) && ok;
        let declared_colls = strings(&intent["collections"]);
        for ckey in &declared_colls {
            if !self.colls_by_key.contains_key(ckey) {
                self.fail(at, format!("no such collection: {ckey}"));
                ok = false;
            }
        }
        let attachment = intent["attachment"].as_str().map(String::from);
        if let Some(attachment) = &attachment {
            ok = self.check_adoptable(at, attachment) && ok;
        }
        if !ok {
            return Ok(());
        }
        // An adopted attachment hands over everything the user said about the
        // file. Zotero surfaces tags on items and forbids child items in
        // collections, so anything left on the child is invisible or silently
        // dropped. Adopted tags skip the registry check on purpose: they already
        // exist in the library, and the registry governs what may be *proposed*,
        // not what is already assigned.
        let (adopted_tags, adopted_colls) = match &attachment {
            Some(attachment) => {
                let adata = self.item_state(attachment);
                let tags = list(adata, "tags")
                    .iter()
                    .map(|t| t["tag"].clone())
                    .collect();
                (tags, list(adata, "collections"))
            }
            None => (Vec::new(), Vec::new()),
        };
        let tags = dedupe(
            declared_tags
                .into_iter()
                .map(Value::String)
                .chain(adopted_tags.iter().cloned()),
        );
        let colls = dedupe(
            declared_colls
                .into_iter()
                .map(Value::String)
                .chain(adopted_colls.iter().cloned()),
        );

        let mut new = Map::new();
        new.insert("itemType".into(), json!(item_type));
        for (name, value) in intent["fields"].as_object().into_iter().flatten() {
            new.insert(name.clone(), value.clone());
        }
        let tag_objects = tags.iter().map(|t| json!({"tag": t, "type": 0})).collect();
        new.insert("tags".into(), Value::Array(tag_objects));
        new.insert("collections".into(), Value::Array(colls));
        if py_truthy(&intent["creators"]) {
            new.insert("creators".into(), intent["creators"].clone());
        }
        let key = self.create(at, "item", Value::Object(new));
        let Some(attachment) = attachment else {
            return Ok(());
        };
        let parent = Change::new("parentItem", json!(false), json!(key), LOW);
        self.apply_item(at, &attachment, "parentItem", parent);
        if !adopted_tags.is_empty() {
            let old = self.item_state(&attachment)["tags"].clone();
            self.apply_item(
                at,
                &attachment,
                "tags",
                Change::new("tags", old, json!([]), LOW),
            );
        }
        if !adopted_colls.is_empty() {
            let change = Change::new("collections", Value::Array(adopted_colls), json!([]), LOW);
            self.apply_item(at, &attachment, "collections", change);
        }
        Ok(())
    }

    fn check_create_fields(
        &mut self,
        at: At,
        item_type: &str,
        valid: &HashSet<String>,
        intent: &Value,
    ) -> Result<bool, Error> {
        let mut ok = true;
        for name in intent["fields"]
            .as_object()
            .into_iter()
            .flatten()
            .map(|(k, _)| k)
        {
            if name == "extra" {
                self.fail(
                    at,
                    format!("'extra' is never a create_item target — {EXTRA}"),
                );
                ok = false;
            } else if !valid.contains(name) {
                let message = format!("{} is not a field of {}", py_repr(name), py_repr(item_type));
                self.fail(at, message);
                ok = false;
            }
        }
        for creator in intent["creators"].as_array().into_iter().flatten() {
            let creator_type = py_str(&creator["creatorType"]);
            if !self
                .client
                .item_type_creator_types(item_type)?
                .contains(&creator_type)
            {
                let message = format!(
                    "{} is not a creator type of {}",
                    py_repr(&creator_type),
                    py_repr(item_type)
                );
                self.fail(at, message);
                ok = false;
            }
        }
        Ok(ok)
    }

    fn check_adoptable(&mut self, at: At, attachment: &str) -> bool {
        if !self.items_by_key.contains_key(attachment) {
            self.fail(at, format!("no such item: {attachment}"));
            return false;
        }
        let data = self.item_state(attachment);
        if data.get("itemType").and_then(Value::as_str) != Some("attachment") {
            self.fail(at, format!("{attachment} is not an attachment"));
            return false;
        }
        if let Some(parent) = data.get("parentItem").filter(|p| py_truthy(p)) {
            let message = format!("{attachment} is already attached to {}", py_str(parent));
            self.fail(at, message);
            return false;
        }
        true
    }

    pub(super) fn set_item_type(&mut self, at: At, intent: &Value) -> Result<(), Error> {
        let (key, new_type) = (text(intent, "key"), text(intent, "itemType"));
        if !self.require_item(at, &key) {
            return Ok(());
        }
        let old_type = py_str(&self.item_state(&key)["itemType"]);
        if old_type == new_type {
            return Ok(());
        }
        let Some(new_valid) = self.fields_of(at, &new_type)? else {
            return Ok(());
        };
        let allowed = self.client.item_type_creator_types(&new_type)?;
        let mut bad: Vec<String> = list(self.item_state(&key), "creators")
            .iter()
            .map(|c| py_str(&c["creatorType"]))
            .filter(|t| !allowed.contains(t))
            .collect::<HashSet<_>>()
            .into_iter()
            .collect();
        if !bad.is_empty() {
            bad.sort();
            let message = format!(
                "creator type(s) {} not valid for {} — set_creators first",
                bad.join(", "),
                py_repr(&new_type)
            );
            self.fail(at, message);
            return Ok(());
        }
        let old_valid = self.client.item_type_fields(&old_type)?;
        let change = Change::new("itemType", json!(old_type), json!(new_type), HIGH);
        self.apply_item(at, &key, "itemType", change);
        // The server validates the merged object against the new type: stored
        // fields it no longer allows must be cleared in the same write.
        let mut stale: Vec<String> = self
            .item_state(&key)
            .keys()
            .filter(|name| old_valid.contains(*name) && !new_valid.contains(*name))
            .cloned()
            .collect();
        stale.sort();
        for name in stale {
            let old = self.item_state(&key)[&name].clone();
            if !py_truthy(&old) {
                continue;
            }
            let change = Change::new(format!("field:{name}"), old, json!(""), HIGH);
            self.apply_item(at, &key, &name, change);
        }
        Ok(())
    }

    pub(super) fn trash_item(&mut self, at: At, intent: &Value) {
        let key = text(intent, "key");
        if !self.require_item(at, &key)
            || self.item_state(&key).get("deleted").is_some_and(py_truthy)
        {
            return;
        }
        let change = Change::new("deleted", json!(false), json!(true), HIGH);
        self.apply_item(at, &key, "deleted", change);
    }
}
