//! `json.loads` with CPython's error messages.
//!
//! `zel validate` reports a changeset that is not JSON as Python's
//! `JSONDecodeError` words it ("Expecting ',' delimiter: line 2 column 5
//! (char 13)"), and serde_json words and positions its errors differently. This
//! scanner walks the text the way CPython's C scanner (`_json.c`, 3.13+) does,
//! only to find the first error; serde_json still builds the value.

use serde_json::Value;

/// Parse JSON text, or return the message `str(JSONDecodeError)` would carry.
///
/// Text CPython accepts but serde_json refuses (`NaN`, `Infinity`, lone
/// surrogate escapes) comes back with serde_json's own message.
pub fn loads(text: &str) -> Result<Value, String> {
    let chars: Vec<char> = text.chars().collect();
    if chars.first() == Some(&'\u{feff}') {
        return Err(message(
            &chars,
            "Unexpected UTF-8 BOM (decode using utf-8-sig)",
            0,
        ));
    }
    let scanner = Scanner { s: &chars };
    let start = scanner.skip_ws(0);
    let end = match scanner.scan_once(start) {
        Ok(end) => scanner.skip_ws(end),
        Err(Fail::StopIteration(at)) => return Err(message(&chars, "Expecting value", at)),
        Err(Fail::Decode(msg, at)) => return Err(message(&chars, msg, at)),
    };
    if end != chars.len() {
        return Err(message(&chars, "Extra data", end));
    }
    serde_json::from_str(text).map_err(|e| e.to_string())
}

/// `JSONDecodeError.__str__`: positions count characters, not bytes.
fn message(s: &[char], msg: &str, pos: usize) -> String {
    let before = &s[..pos];
    let lineno = before.iter().filter(|c| **c == '\n').count() + 1;
    let colno = match before.iter().rposition(|c| *c == '\n') {
        Some(newline) => pos - newline,
        None => pos + 1,
    };
    format!("{msg}: line {lineno} column {colno} (char {pos})")
}

enum Fail {
    /// No value starts here; the caller words it "Expecting value".
    StopIteration(usize),
    Decode(&'static str, usize),
}

struct Scanner<'a> {
    s: &'a [char],
}

impl Scanner<'_> {
    fn at(&self, idx: usize) -> Option<char> {
        self.s.get(idx).copied()
    }

    fn skip_ws(&self, mut idx: usize) -> usize {
        while matches!(self.at(idx), Some(' ' | '\t' | '\n' | '\r')) {
            idx += 1;
        }
        idx
    }

    fn literal(&self, idx: usize, word: &str) -> bool {
        word.chars()
            .enumerate()
            .all(|(i, c)| self.at(idx + i) == Some(c))
    }

    /// One value starting at `idx`; the index just past it.
    fn scan_once(&self, idx: usize) -> Result<usize, Fail> {
        let Some(c) = self.at(idx) else {
            return Err(Fail::StopIteration(idx));
        };
        match c {
            '"' => return self.scan_string(idx + 1),
            '{' => return self.parse_object(idx + 1),
            '[' => return self.parse_array(idx + 1),
            _ => {}
        }
        for word in ["null", "true", "false", "NaN", "Infinity", "-Infinity"] {
            if self.literal(idx, word) {
                return Ok(idx + word.chars().count());
            }
        }
        self.match_number(idx)
    }

    fn match_number(&self, start: usize) -> Result<usize, Fail> {
        let digit = |idx: usize| self.at(idx).is_some_and(|c| c.is_ascii_digit());
        let mut idx = start;
        if self.at(idx) == Some('-') {
            idx += 1;
        }
        match self.at(idx) {
            Some('1'..='9') => {
                while digit(idx) {
                    idx += 1;
                }
            }
            Some('0') => idx += 1,
            _ => return Err(Fail::StopIteration(start)),
        }
        if self.at(idx) == Some('.') && digit(idx + 1) {
            idx += 1;
            while digit(idx) {
                idx += 1;
            }
        }
        if matches!(self.at(idx), Some('e' | 'E')) {
            let e_start = idx;
            idx += 1;
            if matches!(self.at(idx), Some('-' | '+')) {
                idx += 1;
            }
            let digits_from = idx;
            while digit(idx) {
                idx += 1;
            }
            if idx == digits_from {
                idx = e_start; // no exponent digits: the number ends before the 'e'
            }
        }
        Ok(idx)
    }

    /// The body of a string whose opening quote sits at `begin - 1`.
    fn scan_string(&self, begin: usize) -> Result<usize, Fail> {
        let unterminated = Fail::Decode("Unterminated string starting at", begin - 1);
        let len = self.s.len();
        let mut end = begin;
        loop {
            let mut next = end;
            let mut c = None;
            while next < len {
                let d = self.s[next];
                if d == '"' || d == '\\' {
                    c = Some(d);
                    break;
                }
                if (d as u32) <= 0x1f {
                    return Err(Fail::Decode("Invalid control character at", next));
                }
                next += 1;
            }
            match c {
                Some('"') => return Ok(next + 1),
                Some(_) => {}
                None => return Err(unterminated),
            }
            next += 1; // past the backslash
            let Some(escape) = self.at(next) else {
                return Err(unterminated);
            };
            if escape != 'u' {
                if !matches!(escape, '"' | '\\' | '/' | 'b' | 'f' | 'n' | 'r' | 't') {
                    return Err(Fail::Decode("Invalid \\escape", next - 1));
                }
                end = next + 1;
                continue;
            }
            next += 1;
            end = next + 4;
            if end >= len {
                return Err(Fail::Decode("Invalid \\uXXXX escape", next - 1));
            }
            let high = self
                .hex4(next)
                .ok_or(Fail::Decode("Invalid \\uXXXX escape", next - 1))?;
            // A high surrogate followed by another \u escape reads both.
            if (0xd800..0xdc00).contains(&high)
                && end + 6 < len
                && self.at(end) == Some('\\')
                && self.at(end + 1) == Some('u')
            {
                let low = self
                    .hex4(end + 2)
                    .ok_or(Fail::Decode("Invalid \\uXXXX escape", end + 1))?;
                if (0xdc00..0xe000).contains(&low) {
                    end += 6;
                }
            }
        }
    }

    fn hex4(&self, idx: usize) -> Option<u32> {
        (idx..idx + 4).try_fold(0, |acc, i| Some(acc * 16 + self.at(i)?.to_digit(16)?))
    }

    fn parse_object(&self, start: usize) -> Result<usize, Fail> {
        let mut idx = self.skip_ws(start);
        if self.at(idx) == Some('}') {
            return Ok(idx + 1);
        }
        loop {
            if self.at(idx) != Some('"') {
                return Err(Fail::Decode(
                    "Expecting property name enclosed in double quotes",
                    idx,
                ));
            }
            idx = self.skip_ws(self.scan_string(idx + 1)?);
            if self.at(idx) != Some(':') {
                return Err(Fail::Decode("Expecting ':' delimiter", idx));
            }
            idx = self.skip_ws(self.scan_once(self.skip_ws(idx + 1))?);
            match self.at(idx) {
                Some('}') => return Ok(idx + 1),
                Some(',') => {}
                _ => return Err(Fail::Decode("Expecting ',' delimiter", idx)),
            }
            let comma = idx;
            idx = self.skip_ws(idx + 1);
            if self.at(idx) == Some('}') {
                return Err(Fail::Decode(
                    "Illegal trailing comma before end of object",
                    comma,
                ));
            }
        }
    }

    fn parse_array(&self, start: usize) -> Result<usize, Fail> {
        let mut idx = self.skip_ws(start);
        if self.at(idx) == Some(']') {
            return Ok(idx + 1);
        }
        loop {
            idx = self.skip_ws(self.scan_once(idx)?);
            match self.at(idx) {
                Some(']') => return Ok(idx + 1),
                Some(',') => {}
                _ => return Err(Fail::Decode("Expecting ',' delimiter", idx)),
            }
            let comma = idx;
            idx = self.skip_ws(idx + 1);
            if self.at(idx) == Some(']') {
                return Err(Fail::Decode(
                    "Illegal trailing comma before end of array",
                    comma,
                ));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Literal `str(JSONDecodeError)` outputs from CPython 3.14's `json.loads`.
    #[test]
    fn errors_match_cpython() {
        for (text, python) in [
            ("", "Expecting value: line 1 column 1 (char 0)"),
            (" ", "Expecting value: line 1 column 2 (char 1)"),
            (
                "[1,]",
                "Illegal trailing comma before end of array: line 1 column 3 (char 2)",
            ),
            (
                r#"{"a":1,}"#,
                "Illegal trailing comma before end of object: line 1 column 7 (char 6)",
            ),
            (
                r#"{"a" 1}"#,
                "Expecting ':' delimiter: line 1 column 6 (char 5)",
            ),
            (r#"{"a":}"#, "Expecting value: line 1 column 6 (char 5)"),
            (
                "{1:2}",
                "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
            ),
            ("[1 2]", "Expecting ',' delimiter: line 1 column 4 (char 3)"),
            (
                "\"abc",
                "Unterminated string starting at: line 1 column 1 (char 0)",
            ),
            (r#""a\qb""#, "Invalid \\escape: line 1 column 3 (char 2)"),
            (
                r#""a\u12""#,
                "Invalid \\uXXXX escape: line 1 column 4 (char 3)",
            ),
            (
                r#""a\u12zz""#,
                "Invalid \\uXXXX escape: line 1 column 4 (char 3)",
            ),
            (
                "\"a\u{1}\"",
                "Invalid control character at: line 1 column 3 (char 2)",
            ),
            ("{} x", "Extra data: line 1 column 4 (char 3)"),
            ("01", "Extra data: line 1 column 2 (char 1)"),
            ("-", "Expecting value: line 1 column 1 (char 0)"),
            ("1.", "Extra data: line 1 column 2 (char 1)"),
            ("nul", "Expecting value: line 1 column 1 (char 0)"),
            (
                "\u{feff}{}",
                "Unexpected UTF-8 BOM (decode using utf-8-sig): line 1 column 1 (char 0)",
            ),
            (
                "{\"a\":[1,\n  2,,3]}",
                "Expecting value: line 2 column 5 (char 13)",
            ),
            ("[", "Expecting value: line 1 column 2 (char 1)"),
            (
                "{",
                "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
            ),
            (
                "{\"a\"",
                "Expecting ':' delimiter: line 1 column 5 (char 4)",
            ),
            (
                "{\"a\":1,",
                "Expecting property name enclosed in double quotes: line 1 column 8 (char 7)",
            ),
            ("[1,", "Expecting value: line 1 column 4 (char 3)"),
            (
                "\"\\",
                "Unterminated string starting at: line 1 column 1 (char 0)",
            ),
            ("\"\\u", "Invalid \\uXXXX escape: line 1 column 3 (char 2)"),
            (
                r#""\ud800\u12""#,
                "Invalid \\uXXXX escape: line 1 column 9 (char 8)",
            ),
            (
                r#""\ud800\uzzzz""#,
                "Invalid \\uXXXX escape: line 1 column 9 (char 8)",
            ),
            ("{\"a\":1}\n\n x", "Extra data: line 3 column 2 (char 10)"),
            (
                "[\"é\" 1]",
                "Expecting ',' delimiter: line 1 column 6 (char 5)",
            ),
        ] {
            assert_eq!(loads(text).unwrap_err(), python, "{text:?}");
        }
    }

    #[test]
    fn valid_text_parses() {
        let value = loads(r#" {"a": [1, 2.5e3, "😀", true, null]} "#).unwrap();
        assert_eq!(value["a"][2], "😀");
    }
}
