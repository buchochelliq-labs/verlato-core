//! Byte-exact emulation of Python's `json.dumps` for the options Verlato uses.
//!
//! Model request hashes, plugin payloads and size accounting are computed over Python's JSON
//! text (`sort_keys=True`, `ensure_ascii`, default or compact separators), so the native
//! runtime must produce the same bytes to keep request hashes and audits identical.

use serde_json::{Number, Value};
use std::fmt::Write as _;

#[derive(Clone, Copy, Debug)]
pub struct Options {
    pub sort_keys: bool,
    pub ensure_ascii: bool,
    /// `(",", ":")` instead of Python's default `(", ", ": ")`.
    pub compact: bool,
}

/// `json.dumps(value, sort_keys=True)`: default separators, ASCII-only output.
pub const DEFAULT_SORTED: Options = Options {
    sort_keys: true,
    ensure_ascii: true,
    compact: false,
};

/// `json.dumps(value, sort_keys=True, separators=(",", ":"))`.
pub const COMPACT_SORTED_ASCII: Options = Options {
    sort_keys: true,
    ensure_ascii: true,
    compact: true,
};

/// `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
pub const CANONICAL: Options = Options {
    sort_keys: true,
    ensure_ascii: false,
    compact: true,
};

/// `json.dumps(value)` (insertion order, default separators, ASCII-only).
pub const DEFAULT: Options = Options {
    sort_keys: false,
    ensure_ascii: true,
    compact: false,
};

pub fn dumps(value: &Value, options: Options) -> String {
    let mut out = String::new();
    write_value(&mut out, value, options);
    out
}

fn write_value(out: &mut String, value: &Value, options: Options) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(number) => write_number(out, number),
        Value::String(text) => write_string(out, text, options.ensure_ascii),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push_str(if options.compact { "," } else { ", " });
                }
                write_value(out, item, options);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            let mut entries: Vec<(&String, &Value)> = map.iter().collect();
            if options.sort_keys {
                // Python sorts str keys by code point, which matches UTF-8 byte order.
                entries.sort_by(|a, b| a.0.cmp(b.0));
            }
            for (index, (key, item)) in entries.into_iter().enumerate() {
                if index > 0 {
                    out.push_str(if options.compact { "," } else { ", " });
                }
                write_string(out, key, options.ensure_ascii);
                out.push_str(if options.compact { ":" } else { ": " });
                write_value(out, item, options);
            }
            out.push('}');
        }
    }
}

fn write_number(out: &mut String, number: &Number) {
    if let Some(value) = number.as_i64() {
        let _ = write!(out, "{value}");
    } else if let Some(value) = number.as_u64() {
        let _ = write!(out, "{value}");
    } else if let Some(value) = number.as_f64() {
        out.push_str(&float_repr(value));
    }
}

/// Python's `repr(float)`: the shortest round-trip digits, fixed notation for exponents in
/// `[-4, 16)` (always with a fractional part), scientific (`1e-05`, `1.5e+16`) otherwise.
pub fn float_repr(value: f64) -> String {
    if value.is_nan() {
        return "NaN".into();
    }
    if value.is_infinite() {
        return if value > 0.0 { "Infinity" } else { "-Infinity" }.into();
    }
    if value == 0.0 {
        return if value.is_sign_negative() {
            "-0.0"
        } else {
            "0.0"
        }
        .into();
    }
    // Rust's `{:e}` prints the shortest round-trip digits as `d.ddde<exp>`.
    let sci = format!("{value:e}");
    let (mantissa, exponent) = sci.split_once('e').expect("exponent");
    let exponent: i32 = exponent.parse().expect("exponent digits");
    let negative = mantissa.starts_with('-');
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let sign = if negative { "-" } else { "" };
    if (-4..16).contains(&exponent) {
        let point = exponent + 1;
        let text = if point <= 0 {
            format!("0.{}{}", "0".repeat((-point) as usize), digits)
        } else if (point as usize) >= digits.len() {
            format!("{}{}.0", digits, "0".repeat(point as usize - digits.len()))
        } else {
            format!(
                "{}.{}",
                &digits[..point as usize],
                &digits[point as usize..]
            )
        };
        format!("{sign}{text}")
    } else {
        let head = &digits[..1];
        let tail = &digits[1..];
        let mantissa = if tail.is_empty() {
            head.to_owned()
        } else {
            format!("{head}.{tail}")
        };
        let exp_sign = if exponent < 0 { '-' } else { '+' };
        format!("{sign}{mantissa}e{exp_sign}{:02}", exponent.abs())
    }
}

fn write_string(out: &mut String, text: &str, ensure_ascii: bool) {
    out.push('"');
    for ch in text.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c if ensure_ascii && (c as u32) > 0x7e => {
                let mut buffer = [0u16; 2];
                for unit in c.encode_utf16(&mut buffer) {
                    let _ = write!(out, "\\u{:04x}", unit);
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// Python `str.splitlines()`: splits on every line boundary Python recognises.
pub fn splitlines(text: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((index, ch)) = chars.next() {
        let boundary = matches!(
            ch,
            '\n' | '\r'
                | '\u{0b}'
                | '\u{0c}'
                | '\u{1c}'
                | '\u{1d}'
                | '\u{1e}'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        );
        if !boundary {
            continue;
        }
        lines.push(&text[start..index]);
        let mut end = index + ch.len_utf8();
        if ch == '\r' {
            if let Some((next, '\n')) = chars.peek().copied() {
                chars.next();
                end = next + 1;
            }
        }
        start = end;
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }
    lines
}

/// Python `len()` of a string: the number of code points.
pub fn py_len(text: &str) -> usize {
    text.chars().count()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn matches_python_json_dumps() {
        let value = json!({"b": [1, 2.5, null, true], "a": "é\n\"x\"\u{1F600}", "c": {"z": 0.7, "y": 1e-5}});
        assert_eq!(
            dumps(&value, DEFAULT_SORTED),
            r#"{"a": "\u00e9\n\"x\"\ud83d\ude00", "b": [1, 2.5, null, true], "c": {"y": 1e-05, "z": 0.7}}"#
        );
        assert_eq!(
            dumps(&value, CANONICAL),
            "{\"a\":\"é\\n\\\"x\\\"\u{1F600}\",\"b\":[1,2.5,null,true],\"c\":{\"y\":1e-05,\"z\":0.7}}"
        );
    }

    #[test]
    fn float_repr_matches_python() {
        for (value, expected) in [
            (1.0, "1.0"),
            (0.1, "0.1"),
            (0.79, "0.79"),
            (123456.0, "123456.0"),
            (1e16, "1e+16"),
            (1.5e16, "1.5e+16"),
            (1e15, "1000000000000000.0"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (-2.5, "-2.5"),
            (0.0, "0.0"),
        ] {
            assert_eq!(float_repr(value), expected, "{value}");
        }
    }
}
