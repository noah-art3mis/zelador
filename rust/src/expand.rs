//! Expansion (port of write/expand.py): validate symbolic intents and expand
//! them into a version-pinned plan.
//!
//! Every failure is collected, never raised mid-walk – the agent gets the whole
//! list at once. Operations on the same object compose through a working copy
//! of its state, so one plan produces one write per object and the second
//! operation's old state is the first's new state.

use std::collections::{HashMap, HashSet};
use std::hash::{BuildHasher, Hasher, RandomState};

use chrono::{DateTime, Utc};
use indexmap::IndexMap;
use serde_json::{Map, Value, json};

use crate::client::{ItemsQuery, ZoteroClient};
use crate::config::Error;
use crate::contracts::{Changeset, Operation, Plan, plan_id};
use crate::pyjson::{py_eq, py_repr, py_repr_value, py_str, py_truthy};
use crate::taxonomy::Taxonomy;

const ZOTERO_KEY_ALPHABET: &[u8] = b"23456789ABCDEFGHIJKLMNPQRSTUVWXYZ";

pub type Keygen = Box<dyn FnMut() -> String>;

/// A fresh client-side object key. `RandomState` draws its keys from the OS
/// once per thread and steps them for every new instance, so each draw is an
/// independent SipHash output: unpredictable enough for object keys, which
/// need uniqueness, not secrecy.
fn random_key() -> String {
    (0..8)
        .map(|_| {
            let mut hasher = RandomState::new().build_hasher();
            hasher.write_u8(0);
            let index = (hasher.finish() % ZOTERO_KEY_ALPHABET.len() as u64) as usize;
            ZOTERO_KEY_ALPHABET[index] as char
        })
        .collect()
}

/// The key generator `create_item` and `create_collection` draw from. Debug
/// builds honour `ZELADOR_KEYGEN_PREFIX` – keys `<prefix>0001`, `<prefix>0002`,
/// … – so the differential test can compare plans; release builds never do.
pub fn keygen() -> Keygen {
    if cfg!(debug_assertions)
        && let Ok(prefix) = std::env::var("ZELADOR_KEYGEN_PREFIX")
    {
        let mut count = 0;
        return Box::new(move || {
            count += 1;
            format!("{prefix}{count:04}")
        });
    }
    Box::new(random_key)
}

/// Expand a changeset against the live library; `Err(Ok(failures))` on any
/// validation failure, `Err(Err(_))` when the library could not be read.
pub fn expand(
    changeset: &Changeset,
    client: &mut ZoteroClient,
    taxonomy: Option<&Taxonomy>,
    backup: Value,
    now: DateTime<Utc>,
    keygen: Keygen,
) -> Result<Result<Plan, Vec<String>>, Error> {
    let items = client.all_items(ItemsQuery::default())?.unwrap_or_default();
    let collections = client.all_collections()?;
    // Captured before the settings GET – a single-object request whose header
    // carries the setting's own version, not the library version.
    let library_version = client
        .last_modified_version
        .ok_or_else(|| Error::Zotero("response carried no Last-Modified-Version".into()))?;
    let tag_colors = client.setting("tagColors")?;
    let mut expander = Expander::new(client, items, collections, taxonomy, keygen);
    for (group, intent) in changeset.intents.iter().enumerate() {
        expander.expand_intent(group, intent)?;
    }
    expander.check_exclusivity();
    if !expander.failures.is_empty() {
        return Ok(Err(expander.failures));
    }
    Ok(Ok(Plan {
        id: plan_id(&changeset.slug, now),
        slug: changeset.slug.clone(),
        backup,
        library_version,
        intents: changeset.intents.clone(),
        operations: expander.operations,
        settings: settings_drift(taxonomy, tag_colors, library_version),
    }))
}

/// The registry is authoritative for the whole tagColors array – any
/// difference is drift.
fn settings_drift(
    taxonomy: Option<&Taxonomy>,
    live_setting: Option<Value>,
    library_version: i64,
) -> Option<Value> {
    let taxonomy = taxonomy?;
    let live = match live_setting {
        Some(setting) if py_truthy(&setting) => setting["value"].clone(),
        _ => json!([]),
    };
    let registry = taxonomy.tag_colors_value();
    if py_eq(&live, &registry) {
        return None;
    }
    Some(json!({"name": "tagColors", "version": library_version, "old": live, "new": registry}))
}

fn text(intent: &Value, field: &str) -> String {
    intent[field].as_str().unwrap_or_default().to_string()
}

fn strings(value: &Value) -> Vec<String> {
    value
        .as_array()
        .map(|l| {
            l.iter()
                .filter_map(|v| v.as_str().map(String::from))
                .collect()
        })
        .unwrap_or_default()
}

fn tag_of(tag: &Value) -> Option<&str> {
    tag["tag"].as_str()
}

fn tag_entry(tag: &str) -> Value {
    json!({"tag": tag, "type": 0})
}

/// One object's facet write, before it gets its operation id.
struct Write<'a> {
    op: &'a str,
    kind: &'static str,
    key: &'a str,
    version: Value,
    facet: String,
    old: Value,
    new: Value,
    risk: &'static str,
}

struct Expander<'c, 't> {
    client: &'c mut ZoteroClient,
    taxonomy: Option<&'t Taxonomy>,
    keygen: Keygen,
    /// The library's items in API order: merge_tag walks them.
    items: Vec<Value>,
    items_by_key: HashMap<String, Value>,
    colls_by_key: HashMap<String, Value>,
    /// Evolving object data in first-touch order, which the exclusivity
    /// check reports in.
    working_items: IndexMap<String, Map<String, Value>>,
    working_colls: HashMap<String, Map<String, Value>>,
    operations: Vec<Operation>,
    failures: Vec<String>,
}

impl<'c, 't> Expander<'c, 't> {
    fn new(
        client: &'c mut ZoteroClient,
        items: Vec<Value>,
        collections: Vec<Value>,
        taxonomy: Option<&'t Taxonomy>,
        keygen: Keygen,
    ) -> Self {
        let by_key = |objects: &[Value]| {
            objects
                .iter()
                .map(|o| (py_str(&o["key"]), o.clone()))
                .collect()
        };
        Self {
            items_by_key: by_key(&items),
            colls_by_key: by_key(&collections),
            client,
            taxonomy,
            keygen,
            items,
            working_items: IndexMap::new(),
            working_colls: HashMap::new(),
            operations: Vec::new(),
            failures: Vec::new(),
        }
    }

    // -- state ------------------------------------------------------------------

    /// The item's evolving data – ops later in the changeset see earlier results.
    fn item_state(&mut self, key: &str) -> &mut Map<String, Value> {
        let original = &self.items_by_key[key];
        self.working_items
            .entry(key.to_string())
            .or_insert_with(|| original["data"].as_object().cloned().unwrap_or_default())
    }

    fn coll_state(&mut self, key: &str) -> &mut Map<String, Value> {
        let original = &self.colls_by_key[key];
        self.working_colls
            .entry(key.to_string())
            .or_insert_with(|| original["data"].as_object().cloned().unwrap_or_default())
    }

    fn item_tags(&self, key: &str) -> Vec<Value> {
        let tags = match self.working_items.get(key) {
            Some(working) => working.get("tags"),
            None => self.items_by_key[key]["data"].get("tags"),
        };
        tags.and_then(Value::as_array).cloned().unwrap_or_default()
    }

    fn version(&self, key: &str) -> Value {
        self.items_by_key[key]["version"].clone()
    }

    fn emit(&mut self, group: usize, write: Write) {
        let id = format!("op-{:03}", self.operations.len() + 1);
        self.operations.push(Operation {
            id,
            group,
            op: write.op.to_string(),
            kind: write.kind,
            key: write.key.to_string(),
            version: write.version,
            facet: write.facet,
            old: write.old,
            new: write.new,
            risk: write.risk,
        });
    }

    fn fail(&mut self, place: &str, message: impl AsRef<str>) {
        self.failures.push(format!("{place}: {}", message.as_ref()));
    }

    fn require_item(&mut self, key: &str, place: &str) -> bool {
        if !self.items_by_key.contains_key(key) {
            self.fail(place, format!("no such item: {key}"));
            return false;
        }
        self.item_state(key);
        true
    }

    fn require_collection(&mut self, key: &str, place: &str) -> bool {
        if !self.colls_by_key.contains_key(key) {
            self.fail(place, format!("no such collection: {key}"));
            return false;
        }
        self.coll_state(key);
        true
    }

    // -- dispatch ---------------------------------------------------------------

    fn expand_intent(&mut self, group: usize, intent: &Value) -> Result<(), Error> {
        let op = text(intent, "op");
        let place = format!("intent {group} ({op})");
        let place = place.as_str();
        match op.as_str() {
            "merge_tag" => self.merge_tag(group, place, intent),
            "add_tag" => self.add_tag(group, place, intent),
            "remove_tag" => self.remove_tag(group, place, intent),
            "fill_field" => {
                let value = text(intent, "value");
                self.write_field(group, place, intent, &value, "fill_field")?;
            }
            "clear_field" => self.write_field(group, place, intent, "", "clear_field")?,
            "set_creators" => self.set_creators(group, place, intent)?,
            "create_item" => self.create_item(group, place, intent)?,
            "set_item_type" => self.set_item_type(group, place, intent)?,
            "trash_item" => self.trash_item(group, place, intent),
            "add_to_collection" => self.membership(group, place, intent, true),
            "remove_from_collection" => self.membership(group, place, intent, false),
            "create_collection" => self.create_collection(group, place, intent),
            "rename_collection" => self.rename_collection(group, place, intent),
            "move_collection" => self.move_collection(group, place, intent),
            "trash_collection" => self.trash_collection(group, place, intent),
            other => unreachable!("lint admits only known ops, got {other}"),
        }
        Ok(())
    }

    /// True when `tag` may be written. Aliases point at their canonical owner.
    fn canonical_tag(&mut self, tag: &str, place: &str) -> bool {
        let Some(taxonomy) = self.taxonomy else {
            self.fail(place, "writing tags requires taxonomy.yaml");
            return false;
        };
        if taxonomy.is_canonical(tag) {
            return true;
        }
        match taxonomy.alias_owner(tag) {
            Some(owner) => self.fail(
                place,
                format!(
                    "{} is an alias — the canonical tag is {}",
                    py_repr(tag),
                    py_repr(owner)
                ),
            ),
            None => self.fail(
                place,
                format!("{} is not a canonical registry tag", py_repr(tag)),
            ),
        }
        false
    }

    // -- tag ops ----------------------------------------------------------------

    fn merge_tag(&mut self, group: usize, place: &str, intent: &Value) {
        let (sources, target) = (strings(&intent["from"]), text(intent, "into"));
        if !self.canonical_tag(&target, place) {
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
            self.fail(place, message);
            return;
        }
        let is_source = |t: &Value| tag_of(t).is_some_and(|name| sources.iter().any(|s| s == name));
        let keys: Vec<(String, Value)> = self
            .items
            .iter()
            .map(|i| (py_str(&i["key"]), i["version"].clone()))
            .collect();
        for (key, version) in keys {
            let tags = self.item_tags(&key);
            if !tags.iter().any(is_source) {
                continue;
            }
            let mut new: Vec<Value> = tags.iter().filter(|t| !is_source(t)).cloned().collect();
            if !new.iter().any(|t| tag_of(t) == Some(target.as_str())) {
                new.push(tag_entry(&target));
            }
            self.emit(
                group,
                Write {
                    op: "merge_tag",
                    kind: "item",
                    key: &key,
                    version,
                    facet: "tags".into(),
                    old: Value::Array(tags),
                    new: Value::Array(new.clone()),
                    risk: "high",
                },
            );
            self.item_state(&key)
                .insert("tags".into(), Value::Array(new));
        }
    }

    fn add_tag(&mut self, group: usize, place: &str, intent: &Value) {
        let tag = text(intent, "tag");
        if !self.canonical_tag(&tag, place) {
            return;
        }
        for key in strings(&intent["keys"]) {
            if !self.require_item(&key, place) {
                continue;
            }
            let tags = self.item_tags(&key);
            if tags.iter().any(|t| tag_of(t) == Some(tag.as_str())) {
                continue;
            }
            let mut new = tags.clone();
            new.push(tag_entry(&tag));
            let version = self.version(&key);
            self.emit(
                group,
                Write {
                    op: "add_tag",
                    kind: "item",
                    key: &key,
                    version,
                    facet: "tags".into(),
                    old: Value::Array(tags),
                    new: Value::Array(new.clone()),
                    risk: "low",
                },
            );
            self.item_state(&key)
                .insert("tags".into(), Value::Array(new));
        }
    }

    fn remove_tag(&mut self, group: usize, place: &str, intent: &Value) {
        let tag = text(intent, "tag");
        for key in strings(&intent["keys"]) {
            if !self.require_item(&key, place) {
                continue;
            }
            let tags = self.item_tags(&key);
            if !tags.iter().any(|t| tag_of(t) == Some(tag.as_str())) {
                continue;
            }
            let new: Vec<Value> = tags
                .iter()
                .filter(|t| tag_of(t) != Some(tag.as_str()))
                .cloned()
                .collect();
            let version = self.version(&key);
            self.emit(
                group,
                Write {
                    op: "remove_tag",
                    kind: "item",
                    key: &key,
                    version,
                    facet: "tags".into(),
                    old: Value::Array(tags),
                    new: Value::Array(new.clone()),
                    risk: "high",
                },
            );
            self.item_state(&key)
                .insert("tags".into(), Value::Array(new));
        }
    }

    // -- item ops ---------------------------------------------------------------

    fn write_field(
        &mut self,
        group: usize,
        place: &str,
        intent: &Value,
        value: &str,
        op: &str,
    ) -> Result<(), Error> {
        let (key, field) = (text(intent, "key"), text(intent, "field"));
        if !self.require_item(&key, place) {
            return Ok(());
        }
        if field == "extra" {
            self.fail(
                place,
                format!(
                    "'extra' is never a {op} target — it is plugin territory \
                     (Better BibTeX pins citekeys there)"
                ),
            );
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
            self.fail(place, message);
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
        let risk = if py_truthy(&old) { "high" } else { "low" };
        let version = self.version(&key);
        self.emit(
            group,
            Write {
                op,
                kind: "item",
                key: &key,
                version,
                facet: format!("field:{field}"),
                old,
                new: value.clone(),
                risk,
            },
        );
        self.item_state(&key).insert(field, value);
        Ok(())
    }

    fn set_creators(&mut self, group: usize, place: &str, intent: &Value) -> Result<(), Error> {
        let (key, creators) = (text(intent, "key"), intent["creators"].clone());
        if !self.require_item(&key, place) {
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
                self.fail(place, message);
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
        let risk = if py_truthy(&old) { "high" } else { "low" };
        let version = self.version(&key);
        self.emit(
            group,
            Write {
                op: "set_creators",
                kind: "item",
                key: &key,
                version,
                facet: "creators".into(),
                old,
                new: creators.clone(),
                risk,
            },
        );
        self.item_state(&key).insert("creators".into(), creators);
        Ok(())
    }

    fn create_item(&mut self, group: usize, place: &str, intent: &Value) -> Result<(), Error> {
        let item_type = text(intent, "itemType");
        let valid = match self.client.item_type_fields(&item_type) {
            Ok(valid) => valid,
            Err(Error::Zotero(_)) => {
                self.fail(
                    place,
                    format!("{} is not a valid item type", py_repr(&item_type)),
                );
                return Ok(());
            }
            Err(other) => return Err(other),
        };
        let mut ok = self.check_create_fields(place, &item_type, &valid, intent)?;
        let declared_tags = strings(&intent["tags"]);
        ok = self.check_create_tags(place, &declared_tags) && ok;
        let declared_colls = strings(&intent["collections"]);
        for ckey in &declared_colls {
            if !self.colls_by_key.contains_key(ckey) {
                self.fail(place, format!("no such collection: {ckey}"));
                ok = false;
            }
        }
        let attachment = intent["attachment"].as_str().map(String::from);
        if let Some(attachment) = &attachment {
            ok = self.check_adoptable(place, attachment) && ok;
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
        let (mut adopted_tags, mut adopted_colls) = (Vec::new(), Vec::new());
        if let Some(attachment) = &attachment {
            let adata = self.item_state(attachment);
            adopted_tags = adata
                .get("tags")
                .and_then(Value::as_array)
                .map(|tags| tags.iter().map(|t| t["tag"].clone()).collect())
                .unwrap_or_default();
            adopted_colls = adata
                .get("collections")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
        }
        let declared_tags: Vec<Value> = declared_tags.into_iter().map(Value::String).collect();
        let tags = dedupe(
            declared_tags
                .into_iter()
                .chain(adopted_tags.iter().cloned()),
        );
        let declared_colls: Vec<Value> = declared_colls.into_iter().map(Value::String).collect();
        let colls = dedupe(
            declared_colls
                .into_iter()
                .chain(adopted_colls.iter().cloned()),
        );

        let key = (self.keygen)();
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
        let new = Value::Object(new);
        self.emit(
            group,
            Write {
                op: "create_item",
                kind: "item",
                key: &key,
                version: json!(0),
                facet: "object".into(),
                old: Value::Null,
                new: new.clone(),
                risk: "low",
            },
        );
        let mut data = new;
        data["key"] = json!(key);
        self.items_by_key
            .insert(key.clone(), json!({"key": key, "version": 0, "data": data}));
        let Some(attachment) = attachment else {
            return Ok(());
        };
        let version = self.version(&attachment);
        let adopt = |this: &mut Self, facet: &str, old: Value, new: Value| {
            this.emit(
                group,
                Write {
                    op: "create_item",
                    kind: "item",
                    key: &attachment,
                    version: version.clone(),
                    facet: facet.into(),
                    old,
                    new: new.clone(),
                    risk: "low",
                },
            );
            this.item_state(&attachment).insert(facet.into(), new);
        };
        adopt(self, "parentItem", json!(false), json!(key));
        if !adopted_tags.is_empty() {
            let old = self.item_state(&attachment)["tags"].clone();
            adopt(self, "tags", old, json!([]));
        }
        if !adopted_colls.is_empty() {
            adopt(self, "collections", Value::Array(adopted_colls), json!([]));
        }
        Ok(())
    }

    fn check_create_fields(
        &mut self,
        place: &str,
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
                    place,
                    "'extra' is never a create_item target — it is plugin \
                     territory (Better BibTeX pins citekeys there)",
                );
                ok = false;
            } else if !valid.contains(name) {
                let message = format!("{} is not a field of {}", py_repr(name), py_repr(item_type));
                self.fail(place, message);
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
                self.fail(place, message);
                ok = false;
            }
        }
        Ok(ok)
    }

    fn check_create_tags(&mut self, place: &str, tags: &[String]) -> bool {
        // Every tag is checked, so every failure is reported.
        let checked: Vec<bool> = tags.iter().map(|t| self.canonical_tag(t, place)).collect();
        let mut ok = checked.iter().all(|c| *c);
        let Some(taxonomy) = self.taxonomy else {
            return ok;
        };
        for (family, spec) in &taxonomy.families {
            let prefix = format!("{family}:");
            if spec.exclusive && tags.iter().filter(|t| t.starts_with(&prefix)).count() > 1 {
                self.fail(
                    place,
                    format!("would carry multiple '{family}:' tags — family is exclusive"),
                );
                ok = false;
            }
        }
        ok
    }

    fn check_adoptable(&mut self, place: &str, attachment: &str) -> bool {
        if !self.items_by_key.contains_key(attachment) {
            self.fail(place, format!("no such item: {attachment}"));
            return false;
        }
        let data = self.item_state(attachment);
        if data.get("itemType").and_then(Value::as_str) != Some("attachment") {
            self.fail(place, format!("{attachment} is not an attachment"));
            return false;
        }
        if let Some(parent) = data.get("parentItem").filter(|p| py_truthy(p)) {
            let message = format!("{attachment} is already attached to {}", py_str(parent));
            self.fail(place, message);
            return false;
        }
        true
    }

    fn set_item_type(&mut self, group: usize, place: &str, intent: &Value) -> Result<(), Error> {
        let (key, new_type) = (text(intent, "key"), text(intent, "itemType"));
        if !self.require_item(&key, place) {
            return Ok(());
        }
        let old_type = py_str(&self.item_state(&key)["itemType"]);
        if old_type == new_type {
            return Ok(());
        }
        let new_valid = match self.client.item_type_fields(&new_type) {
            Ok(valid) => valid,
            Err(Error::Zotero(_)) => {
                self.fail(
                    place,
                    format!("{} is not a valid item type", py_repr(&new_type)),
                );
                return Ok(());
            }
            Err(other) => return Err(other),
        };
        let allowed = self.client.item_type_creator_types(&new_type)?;
        let mut bad: Vec<String> = self
            .item_state(&key)
            .get("creators")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
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
            self.fail(place, message);
            return Ok(());
        }
        let old_valid = self.client.item_type_fields(&old_type)?;
        let version = self.version(&key);
        self.emit(
            group,
            Write {
                op: "set_item_type",
                kind: "item",
                key: &key,
                version: version.clone(),
                facet: "itemType".into(),
                old: json!(old_type),
                new: json!(new_type),
                risk: "high",
            },
        );
        let data = self.item_state(&key);
        data.insert("itemType".into(), json!(new_type));
        // The server validates the merged object against the new type: stored
        // fields it no longer allows must be cleared in the same write.
        let mut stale: Vec<String> = data
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
            self.emit(
                group,
                Write {
                    op: "set_item_type",
                    kind: "item",
                    key: &key,
                    version: version.clone(),
                    facet: format!("field:{name}"),
                    old,
                    new: json!(""),
                    risk: "high",
                },
            );
            self.item_state(&key).insert(name, json!(""));
        }
        Ok(())
    }

    fn trash_item(&mut self, group: usize, place: &str, intent: &Value) {
        let key = text(intent, "key");
        if !self.require_item(&key, place)
            || self.item_state(&key).get("deleted").is_some_and(py_truthy)
        {
            return;
        }
        let version = self.version(&key);
        self.emit(
            group,
            Write {
                op: "trash_item",
                kind: "item",
                key: &key,
                version,
                facet: "deleted".into(),
                old: json!(false),
                new: json!(true),
                risk: "high",
            },
        );
        self.item_state(&key).insert("deleted".into(), json!(true));
    }

    // -- collection membership -------------------------------------------------

    fn membership(&mut self, group: usize, place: &str, intent: &Value, add: bool) {
        let ckey = text(intent, "collection");
        if !self.colls_by_key.contains_key(&ckey) {
            self.fail(place, format!("no such collection: {ckey}"));
            return;
        }
        let member = json!(ckey);
        for key in strings(&intent["keys"]) {
            if !self.require_item(&key, place) {
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
            let new: Vec<Value> = if add {
                memberships
                    .iter()
                    .cloned()
                    .chain([member.clone()])
                    .collect()
            } else {
                memberships
                    .iter()
                    .filter(|m| !py_eq(m, &member))
                    .cloned()
                    .collect()
            };
            let version = self.version(&key);
            self.emit(
                group,
                Write {
                    op: if add {
                        "add_to_collection"
                    } else {
                        "remove_from_collection"
                    },
                    kind: "item",
                    key: &key,
                    version,
                    facet: "collections".into(),
                    old: Value::Array(memberships),
                    new: Value::Array(new.clone()),
                    risk: if add { "low" } else { "high" },
                },
            );
            self.item_state(&key)
                .insert("collections".into(), Value::Array(new));
        }
    }

    // -- collection objects ----------------------------------------------------

    fn create_collection(&mut self, group: usize, place: &str, intent: &Value) {
        let parent = intent["parent"].as_str().map(String::from);
        if let Some(parent) = &parent
            && !self.colls_by_key.contains_key(parent)
        {
            self.fail(place, format!("no such parent collection: {parent}"));
            return;
        }
        let key = (self.keygen)();
        let new = json!({
            "name": intent["name"],
            "parentCollection": parent.map_or(json!(false), Value::String),
        });
        self.emit(
            group,
            Write {
                op: "create_collection",
                kind: "collection",
                key: &key,
                version: json!(0),
                facet: "object".into(),
                old: Value::Null,
                new: new.clone(),
                risk: "low",
            },
        );
        let mut data = new;
        data["key"] = json!(key);
        self.colls_by_key
            .insert(key.clone(), json!({"key": key, "version": 0, "data": data}));
    }

    fn coll_version(&self, key: &str) -> Value {
        self.colls_by_key[key]["version"].clone()
    }

    fn rename_collection(&mut self, group: usize, place: &str, intent: &Value) {
        let key = text(intent, "collection");
        if !self.require_collection(&key, place) {
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
        let version = self.coll_version(&key);
        self.emit(
            group,
            Write {
                op: "rename_collection",
                kind: "collection",
                key: &key,
                version,
                facet: "name".into(),
                old,
                new: intent["name"].clone(),
                risk: "low",
            },
        );
        self.coll_state(&key)
            .insert("name".into(), intent["name"].clone());
    }

    fn move_collection(&mut self, group: usize, place: &str, intent: &Value) {
        let key = text(intent, "collection");
        if !self.require_collection(&key, place) {
            return;
        }
        let parent = intent["parent"].as_str().map(String::from);
        if let Some(parent) = &parent {
            if !self.colls_by_key.contains_key(parent) {
                self.fail(place, format!("no such parent collection: {parent}"));
                return;
            }
            if self.is_descendant(parent, &key) {
                self.fail(
                    place,
                    format!("{parent} is a descendant of {key} — the move would cycle"),
                );
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
        let version = self.coll_version(&key);
        self.emit(
            group,
            Write {
                op: "move_collection",
                kind: "collection",
                key: &key,
                version,
                facet: "parentCollection".into(),
                old,
                new: new.clone(),
                risk: "high",
            },
        );
        self.coll_state(&key).insert("parentCollection".into(), new);
    }

    fn trash_collection(&mut self, group: usize, place: &str, intent: &Value) {
        let key = text(intent, "collection");
        if !self.require_collection(&key, place)
            || self.coll_state(&key).get("deleted").is_some_and(py_truthy)
        {
            return;
        }
        let version = self.coll_version(&key);
        self.emit(
            group,
            Write {
                op: "trash_collection",
                kind: "collection",
                key: &key,
                version,
                facet: "deleted".into(),
                old: json!(false),
                new: json!(true),
                risk: "high",
            },
        );
        self.coll_state(&key).insert("deleted".into(), json!(true));
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

    // -- cross-cutting checks --------------------------------------------------

    /// A write may not introduce a new exclusive-family violation; stock
    /// violations it leaves untouched are the conformance audit's business,
    /// not a veto.
    fn check_exclusivity(&mut self) {
        let Some(taxonomy) = self.taxonomy else {
            return;
        };
        let exclusive: Vec<&String> = taxonomy
            .families
            .iter()
            .filter(|(_, spec)| spec.exclusive)
            .map(|(name, _)| name)
            .collect();
        let count = |tags: Option<&Value>, prefix: &str| {
            tags.and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter(|t| tag_of(t).is_some_and(|name| name.starts_with(prefix)))
                .count()
        };
        for (key, working) in &self.working_items {
            let original = self.items_by_key[key]["data"].get("tags");
            for family in &exclusive {
                let prefix = format!("{family}:");
                let before = count(original, &prefix);
                let after = count(working.get("tags"), &prefix);
                if after > 1 && after > before {
                    self.failures.push(format!(
                        "item {key}: would carry {after} '{family}:' tags — family is exclusive"
                    ));
                }
            }
        }
    }
}

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
