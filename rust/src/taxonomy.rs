//! Tag taxonomy registry (port of taxonomy.py): families, canonical
//! `family:value` tags, aliases, colours.
//!
//! The registry is linted at load, before anything expands against it – a
//! defective registry fails loudly here, never downstream.

use std::collections::HashSet;
use std::fs;
use std::path::Path;

use indexmap::IndexMap;
use serde_json::{Map, Value, json};
use serde_yaml_ng::Value as Yaml;

use crate::config::Error;
use crate::pyjson::{py_repr, py_repr_value, py_str, py_truthy, re_dollar_body};

/// Zotero renders coloured tags as tinted text in its selector, and this one is
/// unreadable there whatever the palette says (Okabe-Ito yellow).
const ILLEGIBLE_COLOURS: &[&str] = &["#F0E442"];
const COLOURED_CAP: usize = 9; // Zotero pins at most 9 coloured tags

pub struct Family {
    pub exclusive: bool,
    coloured: bool,
}

pub struct TagEntry {
    pub tag: String,
    aliases: Vec<String>,
    /// The raw registry value; a falsy one means uncoloured.
    colour: Value,
}

impl TagEntry {
    fn family(&self) -> &str {
        self.tag.split(':').next().unwrap_or_default()
    }

    fn coloured(&self) -> bool {
        py_truthy(&self.colour)
    }
}

pub struct Taxonomy {
    pub families: IndexMap<String, Family>,
    pub tags: Vec<TagEntry>,
}

impl Taxonomy {
    pub fn is_canonical(&self, name: &str) -> bool {
        self.tags.iter().any(|t| t.tag == name)
    }

    /// The canonical owner of an alias; lint guarantees each alias has one.
    pub fn alias_owner(&self, name: &str) -> Option<&str> {
        self.tags
            .iter()
            .find(|t| t.aliases.iter().any(|a| a == name))
            .map(|t| t.tag.as_str())
    }

    /// The authoritative tagColors setting value; position = declared order.
    pub fn tag_colors_value(&self) -> Value {
        Value::Array(
            self.tags
                .iter()
                .filter(|t| t.coloured())
                .map(|t| json!({"name": t.tag, "color": t.colour}))
                .collect(),
        )
    }
}

/// A YAML node as Python sees what `yaml.safe_load` returns, in JSON terms.
/// Mapping keys become their `str()`.
fn to_json(node: &Yaml) -> Value {
    match node {
        Yaml::Null => Value::Null,
        Yaml::Bool(b) => Value::Bool(*b),
        Yaml::Number(n) => match (n.as_i64(), n.as_u64(), n.as_f64()) {
            (Some(i), _, _) => json!(i),
            (_, Some(u), _) => json!(u),
            (_, _, Some(f)) => serde_json::Number::from_f64(f).map_or(Value::Null, Value::Number),
            _ => Value::Null,
        },
        Yaml::String(s) => Value::String(s.clone()),
        Yaml::Sequence(items) => Value::Array(items.iter().map(to_json).collect()),
        Yaml::Mapping(map) => Value::Object(
            map.iter()
                .map(|(k, v)| (py_str(&to_json(k)), to_json(v)))
                .collect(),
        ),
        Yaml::Tagged(tagged) => to_json(&tagged.value),
    }
}

/// Parse and lint the registry file.
pub fn load_taxonomy(path: &Path) -> Result<Taxonomy, Error> {
    let shown = path.display();
    let fail = |message: String| Err(Error::Config(message));
    if !path.exists() {
        return fail(format!(
            "no taxonomy at {shown} — copy taxonomy.example.yaml to start one"
        ));
    }
    let text = fs::read_to_string(path)?;
    let raw: Yaml = if text.trim().is_empty() {
        Yaml::Null
    } else {
        serde_yaml_ng::from_str(&text).map_err(|e| Error::Config(format!("{shown}: {e}")))?
    };
    let Some(Yaml::Mapping(families_raw)) = raw.get("families") else {
        return fail(format!(
            "{shown} must be a mapping with a 'families' mapping"
        ));
    };
    let Some(Yaml::Sequence(tags_raw)) = raw.get("tags") else {
        return fail(format!("{shown} must declare a 'tags' list"));
    };
    let mut families = IndexMap::new();
    for (name, spec) in families_raw {
        let name_json = to_json(name);
        let spec = to_json(spec);
        let spec = if py_truthy(&spec) {
            spec
        } else {
            Value::Object(Map::new())
        };
        let what = format!("family {}", py_repr_value(&name_json));
        let spec = mapping(
            &spec,
            &what,
            &["coloured", "description", "exclusive"],
            path,
        )?;
        let flag = |key: &str| spec.get(key).is_some_and(py_truthy);
        families.insert(
            py_str(&name_json),
            Family {
                coloured: flag("coloured"),
                exclusive: flag("exclusive"),
            },
        );
    }
    let mut tags = Vec::new();
    for entry in tags_raw {
        let entry = to_json(entry);
        let entry = mapping(
            &entry,
            "tag entry",
            &["aliases", "colour", "description", "tag"],
            path,
        )?;
        tags.push(TagEntry {
            tag: entry.get("tag").map_or_else(String::new, py_str),
            aliases: aliases(entry.get("aliases"), path)?,
            colour: entry.get("colour").cloned().unwrap_or(Value::Null),
        });
    }
    let taxonomy = Taxonomy { families, tags };
    lint(&taxonomy, path)?;
    Ok(taxonomy)
}

/// `tuple(str(a) for a in entry.get("aliases") or ())`: a string iterates by
/// character and a mapping by key, as Python does.
fn aliases(value: Option<&Value>, path: &Path) -> Result<Vec<String>, Error> {
    let Some(value) = value.filter(|v| py_truthy(v)) else {
        return Ok(Vec::new());
    };
    match value {
        Value::Array(items) => Ok(items.iter().map(py_str).collect()),
        Value::String(s) => Ok(s.chars().map(String::from).collect()),
        Value::Object(map) => Ok(map.keys().cloned().collect()),
        other => Err(Error::Config(format!(
            "{}: aliases must be a list, got {}",
            path.display(),
            py_repr_value(other)
        ))),
    }
}

/// A registry node must be a mapping with only known keys – typos fail loudly.
fn mapping<'a>(
    value: &'a Value,
    what: &str,
    allowed: &[&str],
    path: &Path,
) -> Result<&'a Map<String, Value>, Error> {
    let shown = path.display();
    let Some(map) = value.as_object() else {
        return Err(Error::Config(format!(
            "{shown}: {what} must be a mapping, got {}",
            py_repr_value(value)
        )));
    };
    let mut unknown: Vec<&str> = map
        .keys()
        .map(String::as_str)
        .filter(|k| !allowed.contains(k))
        .collect();
    if !unknown.is_empty() {
        unknown.sort();
        return Err(Error::Config(format!(
            "{shown}: unknown key(s) on {what}: {} — allowed: {}",
            unknown.join(", "),
            allowed.join(", ")
        )));
    }
    Ok(map)
}

/// `^[a-z0-9][a-z0-9-]*:[a-z0-9][a-z0-9-]*$` under `re.match`.
fn is_tag_name(tag: &str) -> bool {
    let part = |s: &str| {
        let mut chars = s.chars();
        chars
            .next()
            .is_some_and(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
            && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
    };
    re_dollar_body(tag)
        .split_once(':')
        .is_some_and(|(family, value)| part(family) && part(value))
}

/// `^#[0-9A-Fa-f]{6}$` under `re.match`.
fn is_hex_colour(colour: &str) -> bool {
    re_dollar_body(colour)
        .strip_prefix('#')
        .is_some_and(|hex| hex.len() == 6 && hex.chars().all(|c| c.is_ascii_hexdigit()))
}

fn lint(taxonomy: &Taxonomy, path: &Path) -> Result<(), Error> {
    let shown = path.display();
    let fail = |message: String| Err(Error::Config(format!("{shown}: {message}")));
    let mut seen = HashSet::new();
    for t in &taxonomy.tags {
        if !is_tag_name(&t.tag) {
            return fail(format!(
                "{} is not a lowercase family:value tag",
                py_repr(&t.tag)
            ));
        }
        if !taxonomy.families.contains_key(t.family()) {
            return fail(format!(
                "tag {} uses undeclared family {}",
                py_repr(&t.tag),
                py_repr(t.family())
            ));
        }
        if !seen.insert(t.tag.as_str()) {
            return fail(format!("canonical tag {} is duplicated", py_repr(&t.tag)));
        }
    }
    let mut owner: IndexMap<&str, &str> = IndexMap::new();
    for t in &taxonomy.tags {
        for alias in &t.aliases {
            if seen.contains(alias.as_str()) {
                return fail(format!("alias {} shadows a canonical tag", py_repr(alias)));
            }
            if let Some(first) = owner.get(alias.as_str()) {
                return fail(format!(
                    "alias {} listed under both {} and {}",
                    py_repr(alias),
                    py_repr(first),
                    py_repr(&t.tag)
                ));
            }
            owner.insert(alias, &t.tag);
        }
    }
    let coloured: Vec<&TagEntry> = taxonomy.tags.iter().filter(|t| t.coloured()).collect();
    for t in &coloured {
        let colour = py_repr_value(&t.colour);
        let Some(hex) = t.colour.as_str().filter(|c| is_hex_colour(c)) else {
            return fail(format!(
                "colour {colour} on {} is not #RRGGBB hex",
                py_repr(&t.tag)
            ));
        };
        if ILLEGIBLE_COLOURS.contains(&hex.to_uppercase().as_str()) {
            return fail(format!(
                "colour {colour} on {} is illegible as coloured tag text",
                py_repr(&t.tag)
            ));
        }
        if !taxonomy.families[t.family()].coloured {
            return fail(format!(
                "{} has a colour but family {} is not coloured",
                py_repr(&t.tag),
                py_repr(t.family())
            ));
        }
    }
    if coloured.len() > COLOURED_CAP {
        return fail(format!(
            "{} coloured tags — Zotero pins at most {COLOURED_CAP}",
            coloured.len()
        ));
    }
    Ok(())
}
