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
/// digit (Python ints are unbounded), floats go through `repr`. Relies on
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
    // `{:e}` gives the shortest round-trip digits: "-1.5e-7", "1e16", "0e0".
    let sci = format!("{value:e}");
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
        Value::Number(n) => py_number(n),
        other => other.to_string(),
    }
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
        ] {
            let value: Value = serde_json::from_str(text).unwrap();
            assert_eq!(dumps(&value), python, "{text}");
        }
    }

    #[test]
    fn fmt_g_drops_trailing_zero() {
        assert_eq!(fmt_g(0.0), "0");
        assert_eq!(fmt_g(2.0), "2");
        assert_eq!(fmt_g(0.25), "0.25");
    }
}
