//! Expansion (port of write/expand.py): validate symbolic intents and expand
//! them into a version-pinned plan.
//!
//! Every failure is collected, never raised mid-walk – the agent gets the whole
//! list at once. Operations on the same object compose through a working copy
//! of its state, so one plan produces one write per object and the second
//! operation's old state is the first's new state.
//!
//! The ops live in submodules mirroring Python's sections: `tags`, `items`
//! (item ops), `collections` (membership and collection objects).

mod collections;
mod items;
mod tags;

use std::collections::HashMap;
use std::hash::{BuildHasher, Hasher, RandomState};

use chrono::{DateTime, Utc};
use indexmap::IndexMap;
use serde_json::{Map, Value, json};

use crate::client::{ItemsQuery, ZoteroClient};
use crate::config::Error;
use crate::contracts::{Changeset, Operation, Plan, plan_id};
use crate::pyjson::{py_eq, py_str, py_truthy};
use crate::taxonomy::Taxonomy;

const ZOTERO_KEY_ALPHABET: &[u8] = b"23456789ABCDEFGHIJKLMNPQRSTUVWXYZ";

const LOW: &str = "low";
const HIGH: &str = "high";

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

/// Expand a changeset against the live library; `Ok(Err(failures))` on any
/// validation failure, `Err(_)` when the library could not be read.
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

/// The intent being expanded: its group, its op (which every operation it
/// emits carries) and the prefix its failures are reported under.
#[derive(Clone, Copy)]
struct At<'a> {
    group: usize,
    op: &'a str,
    place: &'a str,
}

/// One facet's change on one object.
struct Change {
    facet: String,
    old: Value,
    new: Value,
    risk: &'static str,
}

impl Change {
    fn new(facet: impl Into<String>, old: Value, new: Value, risk: &'static str) -> Self {
        Self {
            facet: facet.into(),
            old,
            new,
            risk,
        }
    }
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

    // -- operations ---------------------------------------------------------------

    fn record(&mut self, at: At, kind: &'static str, key: &str, version: Value, change: Change) {
        self.operations.push(Operation {
            id: format!("op-{:03}", self.operations.len() + 1),
            group: at.group,
            op: at.op.to_string(),
            kind,
            key: key.to_string(),
            version,
            facet: change.facet,
            old: change.old,
            new: change.new,
            risk: change.risk,
        });
    }

    /// Record a change to an existing item, pinned to its version, and carry
    /// the new value into the item's working state under `field`.
    fn apply_item(&mut self, at: At, key: &str, field: &str, change: Change) {
        let version = self.items_by_key[key]["version"].clone();
        let new = change.new.clone();
        self.record(at, "item", key, version, change);
        self.item_state(key).insert(field.into(), new);
    }

    /// The same for an existing collection.
    fn apply_coll(&mut self, at: At, key: &str, field: &str, change: Change) {
        let version = self.colls_by_key[key]["version"].clone();
        let new = change.new.clone();
        self.record(at, "collection", key, version, change);
        self.coll_state(key).insert(field.into(), new);
    }

    /// Record the creation of an object under a fresh client-side key and make
    /// it visible to the ops that follow.
    fn create(&mut self, at: At, kind: &'static str, new: Value) -> String {
        let key = (self.keygen)();
        self.record(
            at,
            kind,
            &key,
            json!(0),
            Change::new("object", Value::Null, new.clone(), LOW),
        );
        let mut data = new;
        data["key"] = json!(key);
        let object = json!({"key": key, "version": 0, "data": data});
        let pool = if kind == "item" {
            &mut self.items_by_key
        } else {
            &mut self.colls_by_key
        };
        pool.insert(key.clone(), object);
        key
    }

    fn fail(&mut self, at: At, message: impl AsRef<str>) {
        self.failures
            .push(format!("{}: {}", at.place, message.as_ref()));
    }

    fn require_item(&mut self, at: At, key: &str) -> bool {
        if !self.items_by_key.contains_key(key) {
            self.fail(at, format!("no such item: {key}"));
            return false;
        }
        self.item_state(key);
        true
    }

    fn require_collection(&mut self, at: At, key: &str) -> bool {
        if !self.colls_by_key.contains_key(key) {
            self.fail(at, format!("no such collection: {key}"));
            return false;
        }
        self.coll_state(key);
        true
    }

    // -- dispatch ---------------------------------------------------------------

    fn expand_intent(&mut self, group: usize, intent: &Value) -> Result<(), Error> {
        let op = text(intent, "op");
        let place = format!("intent {group} ({op})");
        let at = At {
            group,
            op: &op,
            place: &place,
        };
        match op.as_str() {
            "merge_tag" => self.merge_tag(at, intent),
            "add_tag" => self.add_tag(at, intent),
            "remove_tag" => self.remove_tag(at, intent),
            "fill_field" => self.write_field(at, intent, &text(intent, "value"))?,
            "clear_field" => self.write_field(at, intent, "")?,
            "set_creators" => self.set_creators(at, intent)?,
            "create_item" => self.create_item(at, intent)?,
            "set_item_type" => self.set_item_type(at, intent)?,
            "trash_item" => self.trash_item(at, intent),
            "add_to_collection" => self.membership(at, intent, true),
            "remove_from_collection" => self.membership(at, intent, false),
            "create_collection" => self.create_collection(at, intent),
            "rename_collection" => self.rename_collection(at, intent),
            "move_collection" => self.move_collection(at, intent),
            "trash_collection" => self.trash_collection(at, intent),
            other => unreachable!("lint admits only known ops, got {other}"),
        }
        Ok(())
    }

    // -- cross-cutting checks ---------------------------------------------------

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
