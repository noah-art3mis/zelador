//! `yaml.safe_load` as Python sees it, on top of serde_yaml_ng.
//!
//! PyYAML resolves plain scalars by YAML 1.1 rules, serde_yaml_ng by YAML 1.2:
//! to PyYAML `no`, `On` and `OFF` are booleans, `010` is 8, `1_000` is 1000 and
//! `1:30` is 90, where serde_yaml_ng reads all of them as text. Those flags and
//! names decide whether a registry is refused, so this module re-resolves every
//! plain scalar serde_yaml_ng left as text, and applies merge keys (`<<`) the
//! way PyYAML does.
//!
//! A quoted scalar is never resolved, so the loader must know which text was
//! plain. serde_yaml_ng hands over plain text as a slice of the source starting
//! at the scalar itself and quoted text as a slice starting one character in,
//! so the character after the slice tells them apart: a closing quote, or not.
//!
//! What it cannot re-resolve, it refuses: timestamps (PyYAML builds `date`
//! objects), `.nan` and the `=` value key. Number spellings YAML 1.2 reads as
//! numbers and PyYAML as text (`0o17`, `1e3`, `1.0e3`, `+.5`) reach us without
//! their text and stay numbers – a known difference (rust/README.md).

use std::fmt;

use serde::de::{DeserializeSeed, Deserializer, MapAccess, SeqAccess, Visitor};
use serde_json::{Number, Value};

use crate::pyjson::{py_eq, py_str};

/// A parsed YAML node as Python holds it. Mapping keys keep their type, so a
/// message can show `family 12` rather than `family '12'`.
#[derive(Debug, Clone)]
pub enum Py {
    Scalar(Value),
    List(Vec<Py>),
    Dict(Vec<(Value, Py)>),
}

impl Py {
    /// The value under a string key of a mapping.
    pub fn get(&self, key: &str) -> Option<&Py> {
        let Py::Dict(entries) = self else {
            return None;
        };
        let key = Value::String(key.into());
        entries.iter().find(|(k, _)| py_eq(k, &key)).map(|(_, v)| v)
    }

    /// The JSON form; mapping keys become their `str()`.
    pub fn to_json(&self) -> Value {
        match self {
            Py::Scalar(value) => value.clone(),
            Py::List(items) => Value::Array(items.iter().map(Py::to_json).collect()),
            Py::Dict(entries) => Value::Object(
                entries
                    .iter()
                    .map(|(k, v)| (py_str(k), v.to_json()))
                    .collect(),
            ),
        }
    }
}

/// `yaml.safe_load(text)`; `Err` carries a message for the caller to prefix
/// with the file's path.
pub fn load(text: &str) -> Result<Py, String> {
    // PyYAML reads an empty or comment-only document as None.
    let blank = text
        .lines()
        .all(|l| l.trim().is_empty() || l.trim_start().starts_with('#'));
    if blank {
        return Ok(Py::Scalar(Value::Null));
    }
    let raw = Seed { src: text }
        .deserialize(serde_yaml_ng::Deserializer::from_str(text))
        .map_err(|e| e.to_string())?;
    resolve(raw)
}

/// What serde_yaml_ng reports, before PyYAML's rules are applied.
enum Raw {
    Null,
    Bool(bool),
    Int(String),
    Float(f64),
    Plain(String),
    Quoted(String),
    List(Vec<Raw>),
    Dict(Vec<(Raw, Raw)>),
}

struct Seed<'s> {
    src: &'s str,
}

impl<'de> DeserializeSeed<'de> for Seed<'_> {
    type Value = Raw;

    fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<Raw, D::Error> {
        deserializer.deserialize_any(self)
    }
}

impl<'de> Visitor<'de> for Seed<'_> {
    type Value = Raw;

    fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
        f.write_str("a YAML node")
    }

    fn visit_unit<E>(self) -> Result<Raw, E> {
        Ok(Raw::Null)
    }

    fn visit_none<E>(self) -> Result<Raw, E> {
        Ok(Raw::Null)
    }

    fn visit_bool<E>(self, v: bool) -> Result<Raw, E> {
        Ok(Raw::Bool(v))
    }

    fn visit_i64<E>(self, v: i64) -> Result<Raw, E> {
        Ok(Raw::Int(v.to_string()))
    }

    fn visit_u64<E>(self, v: u64) -> Result<Raw, E> {
        Ok(Raw::Int(v.to_string()))
    }

    fn visit_i128<E>(self, v: i128) -> Result<Raw, E> {
        Ok(Raw::Int(v.to_string()))
    }

    fn visit_u128<E>(self, v: u128) -> Result<Raw, E> {
        Ok(Raw::Int(v.to_string()))
    }

    fn visit_f64<E>(self, v: f64) -> Result<Raw, E> {
        Ok(Raw::Float(v))
    }

    fn visit_borrowed_str<E>(self, v: &'de str) -> Result<Raw, E> {
        let start = (v.as_ptr() as usize).wrapping_sub(self.src.as_ptr() as usize);
        let after = self
            .src
            .get(start + v.len()..)
            .filter(|_| start <= self.src.len())
            .and_then(|rest| rest.chars().next());
        Ok(if matches!(after, Some('\'' | '"')) {
            Raw::Quoted(v.into())
        } else {
            Raw::Plain(v.into())
        })
    }

    /// Text that is not a verbatim slice of the source: a quoted scalar with
    /// escapes, a block scalar, or a plain scalar folded over lines – none of
    /// which PyYAML resolves to anything but text.
    fn visit_str<E>(self, v: &str) -> Result<Raw, E> {
        Ok(Raw::Quoted(v.into()))
    }

    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Raw, A::Error> {
        let mut items = Vec::new();
        while let Some(item) = seq.next_element_seed(Seed { src: self.src })? {
            items.push(item);
        }
        Ok(Raw::List(items))
    }

    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Raw, A::Error> {
        let mut entries = Vec::new();
        while let Some(entry) =
            map.next_entry_seed(Seed { src: self.src }, Seed { src: self.src })?
        {
            entries.push(entry);
        }
        Ok(Raw::Dict(entries))
    }
}

fn number(text: &str) -> Value {
    serde_json::from_str::<Number>(text).map_or(Value::Null, Value::Number)
}

/// A float as a JSON number whose text `py_number` renders as Python's repr.
/// `inf` has no JSON spelling; an overflowing literal stands in for it.
fn float(value: f64) -> Result<Value, String> {
    if value.is_nan() {
        return Err("'.nan' has no equivalent here — remove it".into());
    }
    Ok(if value.is_infinite() {
        number(if value > 0.0 { "1e400" } else { "-1e400" })
    } else {
        Number::from_f64(value).map_or(Value::Null, Value::Number)
    })
}

fn resolve(raw: Raw) -> Result<Py, String> {
    Ok(match raw {
        Raw::Null => Py::Scalar(Value::Null),
        Raw::Bool(b) => Py::Scalar(Value::Bool(b)),
        Raw::Int(text) => Py::Scalar(number(&text)),
        Raw::Float(f) => Py::Scalar(float(f)?),
        Raw::Quoted(text) => Py::Scalar(Value::String(text)),
        Raw::Plain(text) => Py::Scalar(resolve_plain(&text)?),
        Raw::List(items) => Py::List(items.into_iter().map(resolve).collect::<Result<_, _>>()?),
        Raw::Dict(entries) => resolve_dict(entries)?,
    })
}

/// PyYAML's `flatten_mapping` and dict construction: merged pairs come first
/// (from a list of mappings, the last one first), explicit pairs after, and a
/// later duplicate key replaces the value but keeps the first position.
fn resolve_dict(entries: Vec<(Raw, Raw)>) -> Result<Py, String> {
    let mut merged = Vec::new();
    let mut explicit = Vec::new();
    for (key, value) in entries {
        if matches!(&key, Raw::Plain(k) if k == "<<") {
            let sources = match resolve(value)? {
                dict @ Py::Dict(_) => vec![dict],
                Py::List(items) => items,
                _ => return Err("a merge key (<<) needs a mapping or a list of them".into()),
            };
            for source in sources.into_iter().rev() {
                let Py::Dict(pairs) = source else {
                    return Err("a merge key (<<) needs a mapping or a list of them".into());
                };
                merged.extend(pairs);
            }
        } else {
            let Py::Scalar(key) = resolve(key)? else {
                return Err("a mapping key must be a scalar".into());
            };
            explicit.push((key, resolve(value)?));
        }
    }
    let mut out: Vec<(Value, Py)> = Vec::new();
    for (key, value) in merged.into_iter().chain(explicit) {
        match out.iter_mut().find(|(k, _)| py_eq(k, &key)) {
            Some(slot) => slot.1 = value,
            None => out.push((key, value)),
        }
    }
    Ok(Py::Dict(out))
}

/// A plain scalar serde_yaml_ng left as text, through PyYAML's resolvers.
fn resolve_plain(text: &str) -> Result<Value, String> {
    if is_bool(text) {
        return Ok(Value::Bool(matches!(
            text,
            "yes" | "Yes" | "YES" | "true" | "True" | "TRUE" | "on" | "On" | "ON"
        )));
    }
    if is_float(text) {
        return float(construct_float(text)?);
    }
    if is_int(text) {
        return construct_int(text).map(|digits| number(&digits));
    }
    if is_timestamp(text) {
        return Err(format!(
            "{text} is a date to PyYAML — quote it to keep it as text"
        ));
    }
    if text == "=" || text == "<<" {
        return Err(format!("{text} is a YAML 1.1 special key — quote it"));
    }
    Ok(Value::String(text.into()))
}

fn is_bool(text: &str) -> bool {
    matches!(
        text,
        "yes"
            | "Yes"
            | "YES"
            | "no"
            | "No"
            | "NO"
            | "true"
            | "True"
            | "TRUE"
            | "false"
            | "False"
            | "FALSE"
            | "on"
            | "On"
            | "ON"
            | "off"
            | "Off"
            | "OFF"
    )
}

fn sign(text: &str) -> (&str, &str) {
    match text.strip_prefix(['-', '+']) {
        Some(rest) => (&text[..1], rest),
        None => ("", text),
    }
}

fn all(text: &str, ok: impl Fn(char) -> bool) -> bool {
    text.chars().all(ok)
}

fn dec_(c: char) -> bool {
    c.is_ascii_digit() || c == '_'
}

/// `[0-9][0-9_]*`
fn digits_(text: &str) -> bool {
    text.starts_with(|c: char| c.is_ascii_digit()) && all(text, dec_)
}

/// `(:[0-5]?[0-9])+` after the leading part.
fn sexagesimal_tail(parts: &[&str]) -> bool {
    !parts.is_empty()
        && parts.iter().all(|p| {
            let b = p.as_bytes();
            match b {
                [d] => d.is_ascii_digit(),
                [h, d] => (b'0'..=b'5').contains(h) && d.is_ascii_digit(),
                _ => false,
            }
        })
}

/// `[eE][-+][0-9]+`, or nothing.
fn exponent_ok(exp: Option<&str>) -> bool {
    exp.is_none_or(|e| {
        e.strip_prefix(['-', '+'])
            .is_some_and(|d| !d.is_empty() && all(d, |c| c.is_ascii_digit()))
    })
}

fn is_float(text: &str) -> bool {
    let (_, body) = sign(text);
    if matches!(body, ".inf" | ".Inf" | ".INF") || matches!(text, ".nan" | ".NaN" | ".NAN") {
        return true;
    }
    let (mantissa, exp) = match body.split_once(['e', 'E']) {
        Some((m, e)) => (m, Some(e)),
        None => (body, None),
    };
    if let Some((whole, frac)) = mantissa.split_once('.') {
        if !all(frac, dec_) {
            return false;
        }
        // [-+]?[0-9][0-9_]*\.[0-9_]*([eE][-+][0-9]+)?
        if digits_(whole) && exponent_ok(exp) {
            return true;
        }
        // \.[0-9][0-9_]*([eE][-+][0-9]+)?, unsigned
        if whole.is_empty() && body.len() == text.len() && digits_(frac) && exponent_ok(exp) {
            return true;
        }
        // [-+]?[0-9][0-9_]*(:[0-5]?[0-9])+\.[0-9_]*
        let parts: Vec<&str> = whole.split(':').collect();
        return exp.is_none() && digits_(parts[0]) && sexagesimal_tail(&parts[1..]);
    }
    false
}

fn is_int(text: &str) -> bool {
    let (_, body) = sign(text);
    if let Some(bits) = body.strip_prefix("0b") {
        return !bits.is_empty() && all(bits, |c| matches!(c, '0' | '1' | '_'));
    }
    if let Some(hex) = body.strip_prefix("0x") {
        return !hex.is_empty() && all(hex, |c| c.is_ascii_hexdigit() || c == '_');
    }
    if let Some(oct) = body.strip_prefix('0') {
        return oct.is_empty() || all(oct, |c| matches!(c, '0'..='7' | '_'));
    }
    let parts: Vec<&str> = body.split(':').collect();
    let first = parts[0];
    let decimal = first.starts_with(|c: char| ('1'..='9').contains(&c)) && all(first, dec_);
    decimal && (parts.len() == 1 || sexagesimal_tail(&parts[1..]))
}

/// PyYAML's timestamp resolver: `YYYY-MM-DD`, or `YYYY-M-D` followed by a
/// time. Anything after the date counts, which refuses a little more than
/// PyYAML would – failing closed.
fn is_timestamp(text: &str) -> bool {
    let digits = |s: &str, lo: usize, hi: usize| {
        (lo..=hi).contains(&s.len()) && s.bytes().all(|b| b.is_ascii_digit())
    };
    let mut parts = text.splitn(3, '-');
    let (Some(year), Some(month), Some(rest)) = (parts.next(), parts.next(), parts.next()) else {
        return false;
    };
    let day_len = rest.bytes().take_while(u8::is_ascii_digit).count();
    let (day, time) = rest.split_at(day_len);
    if !digits(year, 4, 4) || !digits(day, 1, 2) || !digits(month, 1, 2) {
        return false;
    }
    if time.is_empty() {
        return month.len() == 2 && day.len() == 2;
    }
    time.starts_with(['T', 't', ' ', '\t'])
}

/// `construct_yaml_int`, as the decimal digits of the result.
fn construct_int(text: &str) -> Result<String, String> {
    let cleaned = text.replace('_', "");
    let (sign, body) = sign(&cleaned);
    let overflow = || format!("{text} is too large an integer for this port");
    let magnitude: u128 = if body == "0" {
        0
    } else if let Some(bits) = body.strip_prefix("0b") {
        u128::from_str_radix(bits, 2).map_err(|_| overflow())?
    } else if let Some(hex) = body.strip_prefix("0x") {
        u128::from_str_radix(hex, 16).map_err(|_| overflow())?
    } else if let Some(oct) = body.strip_prefix('0') {
        u128::from_str_radix(oct, 8).map_err(|_| overflow())?
    } else if body.contains(':') {
        body.split(':').try_fold(0u128, |acc, part| {
            let digit: u128 = part.parse().map_err(|_| overflow())?;
            acc.checked_mul(60)
                .and_then(|v| v.checked_add(digit))
                .ok_or_else(overflow)
        })?
    } else {
        // Plain decimal digits: keep every digit, as Python's int does.
        return Ok(format!(
            "{}{}",
            if sign == "-" { "-" } else { "" },
            body.trim_start_matches('0')
        ));
    };
    Ok(if sign == "-" && magnitude != 0 {
        format!("-{magnitude}")
    } else {
        magnitude.to_string()
    })
}

/// `construct_yaml_float`.
fn construct_float(text: &str) -> Result<f64, String> {
    let cleaned = text.replace('_', "").to_lowercase();
    let (sign, body) = sign(&cleaned);
    let factor = if sign == "-" { -1.0 } else { 1.0 };
    if body == ".inf" {
        return Ok(factor * f64::INFINITY);
    }
    if body == ".nan" {
        return Ok(f64::NAN);
    }
    let bad = || format!("{text} is not a number this port can read");
    if body.contains(':') {
        let mut value = 0.0;
        for part in body.split(':') {
            value = value * 60.0 + part.parse::<f64>().map_err(|_| bad())?;
        }
        return Ok(factor * value);
    }
    let body = if body.ends_with('.') {
        format!("{body}0")
    } else {
        body.to_string()
    };
    Ok(factor * body.parse::<f64>().map_err(|_| bad())?)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pyjson::py_repr_value;

    fn repr(text: &str) -> String {
        py_repr_value(&load(text).unwrap().to_json())
    }

    /// Literal `repr(yaml.safe_load(text))` outputs from PyYAML.
    #[test]
    fn plain_scalars_resolve_like_pyyaml() {
        assert_eq!(
            repr(
                "a: [yes, 'no', NO, y, n, On, 010, 0_0, 1:30, +1, -0x1F, 0b101, 1_000, 1., \
                 .inf, -.Inf, .5, 1_0.5, 1:30.5, 0x_1F, 0b1_0, \
                 123456789012345678901234567890, ~, '']"
            ),
            "{'a': [True, 'no', False, 'y', 'n', True, 8, 0, 90, 1, -31, 5, 1000, 1.0, \
             inf, -inf, 0.5, 10.5, 90.5, 31, 2, 123456789012345678901234567890, None, '']}"
        );
    }

    #[test]
    fn merge_keys_follow_pyyaml() {
        assert_eq!(
            repr("a: {<<: [{x: 1, y: 1}, {y: 2, z: 2}], w: 0}"),
            "{'a': {'y': 1, 'z': 2, 'x': 1, 'w': 0}}"
        );
        assert_eq!(repr("a: {'<<': 1}"), "{'a': {'<<': 1}}");
        assert_eq!(
            repr("b: &f {x: 1}\na: {<<: *f, x: 2}"),
            "{'b': {'x': 1}, 'a': {'x': 2}}"
        );
    }

    #[test]
    fn duplicate_keys_keep_first_position_and_last_value() {
        assert_eq!(repr("a: 1\nb: 2\na: 3"), "{'a': 3, 'b': 2}");
    }

    #[test]
    fn blank_documents_are_none() {
        assert_eq!(repr(""), "None");
        assert_eq!(repr("# only a comment\n"), "None");
    }

    #[test]
    fn unreadable_forms_are_refused() {
        assert!(load("a: 2020-01-01").is_err());
        assert!(load("a: 2020-1-1 10:00:00").is_err());
        assert_eq!(
            repr("a: [2020-1-1, 2020-01-01x]"),
            "{'a': ['2020-1-1', '2020-01-01x']}"
        );
        assert!(load("a: .nan").is_err());
    }
}
