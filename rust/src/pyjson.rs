//! Python-compatible text output.
//!
//! Agents consume `--json` byte-for-byte, so the port reproduces what Python's
//! `json.dumps`, `repr()`, `str()` and `:g` formatting actually emit rather than
//! Rust's equivalents, which differ in separators, quoting and escaping.

use serde_json::Value;
use unicode_general_category::{GeneralCategory, get_general_category};

/// `json.dumps(obj, ensure_ascii=False)`: `", "` and `": "` separators.
pub fn dumps(value: &Value) -> String {
    let mut out = String::new();
    write(&mut out, value, None, 0);
    out
}

/// `json.dumps(obj, indent=2, ensure_ascii=False)`.
pub fn dumps_indent(value: &Value) -> String {
    let mut out = String::new();
    write(&mut out, value, Some(2), 0);
    out
}

fn write(out: &mut String, value: &Value, indent: Option<usize>, level: usize) {
    // With an indent, Python's item separator drops its trailing space.
    let separator = if indent.is_some() { "," } else { ", " };
    match value {
        Value::Array(items) if items.is_empty() => out.push_str("[]"),
        Value::Object(map) if map.is_empty() => out.push_str("{}"),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(separator);
                }
                newline(out, indent, level + 1);
                write(out, item, indent, level + 1);
            }
            newline(out, indent, level);
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            for (i, (key, item)) in map.iter().enumerate() {
                if i > 0 {
                    out.push_str(separator);
                }
                newline(out, indent, level + 1);
                out.push_str(&quote(key));
                out.push_str(": ");
                write(out, item, indent, level + 1);
            }
            newline(out, indent, level);
            out.push('}');
        }
        Value::String(s) => out.push_str(&quote(s)),
        Value::Number(n) => out.push_str(&py_number(n)),
        other => out.push_str(&other.to_string()),
    }
}

fn newline(out: &mut String, indent: Option<usize>, level: usize) {
    if let Some(width) = indent {
        out.push('\n');
        out.push_str(&" ".repeat(width * level));
    }
}

/// A JSON string literal; serde_json escapes exactly the set Python does with
/// ensure_ascii=False (quote, backslash, C0 controls with the short forms).
fn quote(s: &str) -> String {
    serde_json::to_string(s).expect("strings always serialise")
}

/// What Python prints for a JSON number after `json.loads`: integers keep every
/// digit (Python's `json.loads` refuses past 4300; this does not), floats go
/// through `repr`. Relies on
/// serde_json's `arbitrary_precision`, which keeps the number's source text.
pub fn py_number(number: &serde_json::Number) -> String {
    let text = number.as_str();
    if !text.contains(['.', 'e', 'E']) {
        return if text == "-0" {
            "0".into()
        } else {
            text.to_string()
        };
    }
    float_repr(
        text.parse::<f64>()
            .expect("serde_json validated the number"),
    )
}

/// Python's `repr(float)`: the shortest digits that round-trip, in fixed
/// notation when the decimal exponent is in [-4, 16), else `d.ddde±XX`.
fn float_repr(value: f64) -> String {
    if value.is_infinite() {
        return if value > 0.0 { "Infinity" } else { "-Infinity" }.into();
    }
    // `{:e}` finds how many digits the shortest round-trip form needs, but when
    // two candidates of that length are equally close it rounds the tie up,
    // where CPython rounds half to even. Re-render at that length with exact
    // rounding, which lands on CPython's digits: "-1.5e-7", "1e16", "0e0".
    let shortest = format!("{value:e}");
    let length = shortest
        .split('e')
        .next()
        .map_or(1, |m| m.chars().filter(char::is_ascii_digit).count());
    let sci = format!("{:.*e}", length.saturating_sub(1), value);
    let (mantissa, exponent) = sci.split_once('e').expect("{:e} always has an exponent");
    let exponent: i32 = exponent.parse().expect("integer exponent");
    let (sign, mantissa) = match mantissa.strip_prefix('-') {
        Some(rest) => ("-", rest),
        None => ("", mantissa),
    };
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    if !(-4..16).contains(&exponent) {
        let (first, rest) = digits.split_at(1);
        let fraction = if rest.is_empty() {
            String::new()
        } else {
            format!(".{rest}")
        };
        let exp_sign = if exponent < 0 { '-' } else { '+' };
        return format!("{sign}{first}{fraction}e{exp_sign}{:02}", exponent.abs());
    }
    let point = exponent + 1; // digits before the decimal point
    let body = if point <= 0 {
        format!("0.{}{digits}", "0".repeat((-point) as usize))
    } else if point as usize >= digits.len() {
        format!("{digits}{}.0", "0".repeat(point as usize - digits.len()))
    } else {
        let (whole, fraction) = digits.split_at(point as usize);
        format!("{whole}.{fraction}")
    };
    format!("{sign}{body}")
}

/// What an f-string interpolation of a parsed JSON value prints.
pub fn py_str(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Null => "None".into(),
        // str(float("inf")) is "inf"; json.dumps writes "Infinity".
        Value::Number(n) => match py_number(n).as_str() {
            "Infinity" => "inf".into(),
            "-Infinity" => "-inf".into(),
            text => text.into(),
        },
        other => py_repr_value(other),
    }
}

/// Python's `repr()` of what `json.loads` returns: dicts, lists, strings,
/// numbers, `True`/`False` and `None`, nested – as error messages show a value.
pub fn py_repr_value(value: &Value) -> String {
    match value {
        Value::String(s) => py_repr(s),
        Value::Array(items) => {
            let inner: Vec<String> = items.iter().map(py_repr_value).collect();
            format!("[{}]", inner.join(", "))
        }
        Value::Object(map) => {
            let inner: Vec<String> = map
                .iter()
                .map(|(k, v)| format!("{}: {}", py_repr(k), py_repr_value(v)))
                .collect();
            format!("{{{}}}", inner.join(", "))
        }
        scalar => py_str(scalar),
    }
}

/// Python's `==` on parsed JSON: dicts compare without regard to key order,
/// numbers by exact value (`1 == 1.0`, but `2**53 + 1 != 2.0**53`), and
/// `True`/`False` equal `1`/`0`.
pub fn py_eq(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Array(x), Value::Array(y)) => {
            x.len() == y.len() && x.iter().zip(y).all(|(p, q)| py_eq(p, q))
        }
        (Value::Object(x), Value::Object(y)) => {
            x.len() == y.len() && x.iter().all(|(k, v)| y.get(k).is_some_and(|w| py_eq(v, w)))
        }
        (Value::Number(_) | Value::Bool(_), Value::Number(_) | Value::Bool(_)) => {
            match (as_int(a), as_int(b)) {
                (Some(x), Some(y)) => x == y,
                (Some(int), None) => float_is_int(as_float(b), &int),
                (None, Some(int)) => float_is_int(as_float(a), &int),
                (None, None) => as_float(a) == as_float(b),
            }
        }
        _ => a == b,
    }
}

/// The integer text of an int or bool, normalised like `py_number`.
fn as_int(value: &Value) -> Option<String> {
    match value {
        Value::Bool(b) => Some(if *b { "1" } else { "0" }.into()),
        Value::Number(n) if !n.as_str().contains(['.', 'e', 'E']) => Some(py_number(n)),
        _ => None,
    }
}

/// `float == int` in Python: exact, so a float equals an int only when it is
/// finite, whole, and its exact decimal value has the same digits.
fn float_is_int(float: Option<f64>, int: &str) -> bool {
    let Some(f) = float.filter(|f| f.is_finite() && f.fract() == 0.0) else {
        return false;
    };
    let digits = format!("{f:.0}");
    let digits = if digits == "-0" { "0".into() } else { digits };
    digits == int
}

fn as_float(value: &Value) -> Option<f64> {
    match value {
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        Value::Number(n) => n.as_str().parse().ok(),
        _ => None,
    }
}

/// Python truthiness of a parsed JSON value.
pub fn py_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_str().parse::<f64>().is_ok_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

/// The text a `re.match(r"^...$", s)` pattern must cover: Python's `$` also
/// matches just before one trailing newline, so `"slug\n"` passes `^[a-z]+$`.
pub fn re_dollar_body(s: &str) -> &str {
    s.strip_suffix('\n').unwrap_or(s)
}

/// Python's `repr(str)`: prefer single quotes, switch to double quotes when the
/// text holds a single quote and no double quote, escape non-printables.
pub fn py_repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::from(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if !is_printable(c) => {
                let code = c as u32;
                if code < 0x100 {
                    out.push_str(&format!("\\x{code:02x}"));
                } else if code < 0x10000 {
                    out.push_str(&format!("\\u{code:04x}"));
                } else {
                    out.push_str(&format!("\\U{code:08x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// `str.isprintable()` per character: everything except the Unicode "Other"
/// and "Separator" categories, with the ASCII space let through.
fn is_printable(c: char) -> bool {
    use GeneralCategory::*;
    c == ' '
        || !matches!(
            get_general_category(c),
            Control
                | Format
                | Surrogate
                | PrivateUse
                | Unassigned
                | LineSeparator
                | ParagraphSeparator
                | SpaceSeparator
        )
}

/// `f"{value:g}"` for the waits the client announces (whole seconds or short
/// fractions); not a general implementation of the `g` presentation type.
pub fn fmt_g(value: f64) -> String {
    if value.fract() == 0.0 && value.abs() < 1e6 {
        format!("{}", value as i64)
    } else {
        format!("{value}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn compact_dumps_uses_python_separators() {
        let value = json!({"a": [1, "é"], "b": {}, "c": null});
        assert_eq!(dumps(&value), r#"{"a": [1, "é"], "b": {}, "c": null}"#);
    }

    #[test]
    fn indented_dumps_matches_python_layout() {
        let value = json!({"a": [1, 2], "b": []});
        assert_eq!(
            dumps_indent(&value),
            "{\n  \"a\": [\n    1,\n    2\n  ],\n  \"b\": []\n}"
        );
    }

    #[test]
    fn repr_chooses_quotes_like_python() {
        assert_eq!(py_repr("plain"), "'plain'");
        assert_eq!(py_repr("rui's"), "\"rui's\"");
        assert_eq!(py_repr("say \"hi\" it's"), "'say \"hi\" it\\'s'");
        assert_eq!(
            py_repr("tab\there\u{a0}\u{200b}"),
            "'tab\\there\\xa0\\u200b'"
        );
    }

    /// What `json.dumps(json.loads(text))` prints for each JSON number.
    #[test]
    fn numbers_reserialise_like_python() {
        for (text, python) in [
            ("0", "0"),
            ("-0", "0"),
            ("42", "42"),
            ("123456789012345678901234", "123456789012345678901234"),
            ("-9223372036854775809", "-9223372036854775809"),
            ("1.0", "1.0"),
            ("-0.0", "-0.0"),
            ("1.5", "1.5"),
            ("0.0001", "0.0001"),
            ("0.00001", "1e-05"),
            ("2.5e-5", "2.5e-05"),
            ("1.5e-7", "1.5e-07"),
            ("1e5", "100000.0"),
            ("1E5", "100000.0"),
            ("1e15", "1000000000000000.0"),
            ("1e16", "1e+16"),
            ("1.25e100", "1.25e+100"),
            ("1e400", "Infinity"),
            ("-1e400", "-Infinity"),
            ("0.1", "0.1"),
            ("123.456", "123.456"),
            // Exact ties between two shortest candidates: CPython rounds half to even.
            ("1125899906842624.25", "1125899906842624.2"),
            ("-1113178120592002.25", "-1113178120592002.2"),
            ("111659285584252.125", "111659285584252.12"),
        ] {
            let value: Value = serde_json::from_str(text).unwrap();
            assert_eq!(dumps(&value), python, "{text}");
        }
    }

    /// Literal outputs of `repr(json.loads(text))` from CPython 3.14.
    #[test]
    fn repr_value_matches_python_repr_of_parsed_json() {
        for (text, python) in [
            (r#"{"a": true, "b": null}"#, "{'a': True, 'b': None}"),
            (
                "[1, 2.5, -0, -0.0, 1e400, -1e400, 1E5, 0.00001, 123456789012345678901234]",
                "[1, 2.5, 0, -0.0, inf, -inf, 100000.0, 1e-05, 123456789012345678901234]",
            ),
            (r#""rui's""#, r#""rui's""#),
            (
                r#"{"say \"hi\"": ["x", {"k": false}]}"#,
                r#"{'say "hi"': ['x', {'k': False}]}"#,
            ),
            ("[]", "[]"),
            ("{}", "{}"),
            (r#"[[], {}, "\u00e9\t\u200b"]"#, r"[[], {}, 'é\t\u200b']"),
            (r#""it's \"q\"""#, r#"'it\'s "q"'"#),
            ("null", "None"),
            ("true", "True"),
            ("3", "3"),
            ("1.0", "1.0"),
        ] {
            let value: Value = serde_json::from_str(text).unwrap();
            assert_eq!(py_repr_value(&value), python, "{text}");
        }
    }

    /// `str()` differs from `repr()` only for a top-level string.
    #[test]
    fn str_of_containers_is_their_repr() {
        let value: Value = serde_json::from_str(r#"["rui's", {"a": null}]"#).unwrap();
        assert_eq!(py_str(&value), r#"["rui's", {'a': None}]"#);
        assert_eq!(py_str(&Value::String("rui's".into())), "rui's");
    }

    #[test]
    fn equality_follows_python() {
        let parse = |t: &str| serde_json::from_str::<Value>(t).unwrap();
        assert!(py_eq(
            &parse(r#"{"a": 1, "b": [true]}"#),
            &parse(r#"{"b": [1.0], "a": 1}"#)
        ));
        assert!(py_eq(&parse("-0"), &parse("0.0")));
        assert!(!py_eq(
            &parse(r#"[{"a": 1}]"#),
            &parse(r#"[{"a": 1, "b": 2}]"#)
        ));
        assert!(!py_eq(&parse("null"), &parse("false")));
        assert!(!py_eq(&parse(r#""1""#), &parse("1")));
    }

    /// Python compares an int with a float exactly, not through a lossy cast.
    #[test]
    fn int_and_float_compare_exactly() {
        let parse = |t: &str| serde_json::from_str::<Value>(t).unwrap();
        assert!(!py_eq(&parse("9007199254740993"), &parse("9007199254740992.0")));
        assert!(py_eq(&parse("9007199254740992"), &parse("9007199254740992.0")));
        assert!(py_eq(&parse("100000000000000000000"), &parse("1e20")));
        assert!(!py_eq(&parse("1"), &parse("1.5")));
        assert!(!py_eq(&parse("1"), &parse("1e400")));
        assert!(py_eq(&parse("true"), &parse("1.0")));
    }

    #[test]
    fn fmt_g_drops_trailing_zero() {
        assert_eq!(fmt_g(0.0), "0");
        assert_eq!(fmt_g(2.0), "2");
        assert_eq!(fmt_g(0.25), "0.25");
    }
}
