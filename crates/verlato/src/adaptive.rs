//! Opt-in, recommendation-only tuning advice for suggestion settings (ports
//! `_build_suggestion_adaptive_recommendations` and helpers from `verlato/cli/main.py`).

use serde_json::{json, Map, Value};
use std::collections::BTreeMap;

/// Python `round(value, digits)` (correctly rounded, ties to even).
fn round_to(value: f64, digits: usize) -> f64 {
    format!("{value:.digits$}").parse().unwrap_or(value)
}

/// `_optional_float`.
fn optional_float(value: Option<&Value>) -> Option<f64> {
    match value? {
        Value::Number(n) => n.as_f64(),
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        Value::String(s) if !s.trim().is_empty() => s.trim().parse().ok(),
        _ => None,
    }
}

/// `_optional_int`.
fn optional_int(value: Option<&Value>) -> Option<i64> {
    optional_float(value).map(|f| f.trunc() as i64)
}

/// Python `isinstance(value, (int, float))` (bools included).
fn numeric(value: Option<&Value>) -> Option<f64> {
    match value? {
        Value::Number(n) => n.as_f64(),
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        _ => None,
    }
}

fn ratio(value: i64, total: i64) -> f64 {
    if total <= 0 {
        0.0
    } else {
        value as f64 / total as f64
    }
}

/// `_suggestion_adaptive_current_settings`.
pub fn current_settings(
    provider: &str,
    prompt_profile: &str,
    suggestion_count: i64,
    suggestion_parallelism: i64,
    suggestion_batch_size: i64,
    suggestion_strategy: Option<&str>,
    cache_warming: Option<bool>,
) -> Value {
    json!({
        "provider": provider,
        "prompt_profile": prompt_profile,
        "suggestion_count": suggestion_count,
        "suggestion_parallelism": suggestion_parallelism,
        "suggestion_batch_size": suggestion_batch_size,
        "suggestion_strategy": suggestion_strategy,
        "cache_warming": cache_warming,
    })
}

const TIMING_KEYS: [&str; 5] = [
    "model_ms",
    "queue_ms",
    "load_ms",
    "prompt_eval_ms",
    "eval_ms",
];

/// `_adaptive_timing_signals`.
fn timing_signals(sources: &[Value]) -> Map<String, Value> {
    let mut totals: BTreeMap<&str, i64> = TIMING_KEYS.iter().map(|k| (*k, 0)).collect();
    let mut rates: Vec<f64> = Vec::new();
    for source in sources {
        let Some(timings) = source.get("timings").and_then(Value::as_object) else {
            continue;
        };
        for key in TIMING_KEYS {
            if let Some(value) = numeric(timings.get(key)) {
                *totals.get_mut(key).expect("timing key") += value.trunc() as i64;
            }
        }
        if let Some(rate) = numeric(timings.get("tokens_per_second")).filter(|r| *r > 0.0) {
            rates.push(rate);
        }
    }
    let component =
        totals["queue_ms"] + totals["load_ms"] + totals["prompt_eval_ms"] + totals["eval_ms"];
    let model_ms = totals["model_ms"].max(component);
    totals.insert("model_ms", model_ms);
    let candidates = [
        ("queue", totals["queue_ms"]),
        ("load", totals["load_ms"]),
        ("prompt_eval", totals["prompt_eval_ms"]),
        ("generation", totals["eval_ms"]),
    ];
    // Python `max` keeps the first of equal maxima.
    let dominant = if candidates.iter().any(|(_, v)| *v != 0) {
        candidates
            .iter()
            .fold(
                candidates[0],
                |best, item| if item.1 > best.1 { *item } else { best },
            )
            .0
    } else {
        "unknown"
    };
    let mut signals = Map::new();
    for key in TIMING_KEYS {
        signals.insert(key.into(), json!(totals[key]));
    }
    for (name, key) in [
        ("queue_share", "queue_ms"),
        ("load_share", "load_ms"),
        ("prompt_eval_share", "prompt_eval_ms"),
        ("generation_share", "eval_ms"),
    ] {
        signals.insert(
            name.into(),
            json!(round_to(ratio(totals[key], model_ms), 3)),
        );
    }
    signals.insert("dominant_bottleneck".into(), json!(dominant));
    signals.insert(
        "average_tokens_per_second".into(),
        if rates.is_empty() {
            Value::Null
        } else {
            json!(round_to(rates.iter().sum::<f64>() / rates.len() as f64, 1))
        },
    );
    signals
}

const POSITIVE: &[&str] = &[
    "suggestion_applied",
    "summary_edited",
    "suggestion_marked_useful",
];
const NEGATIVE: &[&str] = &[
    "suggestion_skipped",
    "suggestion_rejected",
    "suggestion_not_useful",
    "suggestion_marked_not_useful",
    "suggestion_regenerated",
];

/// Python `str(event.get(key) or default)`.
fn text_or(value: Option<&Value>, default: &str) -> String {
    match value {
        None | Some(Value::Null) | Some(Value::Bool(false)) => default.into(),
        Some(Value::String(s)) if s.is_empty() => default.into(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Bool(true)) => "True".into(),
        Some(Value::Number(n)) if n.as_f64() == Some(0.0) => default.into(),
        Some(other) => other.to_string(),
    }
}

fn rate(count: i64, total: i64) -> Value {
    if total == 0 {
        Value::Null
    } else {
        json!(round_to(count as f64 / total as f64, 3))
    }
}

/// `_adaptive_feedback_signals` (with `_adaptive_source_feedback`).
fn feedback_signals(events: &[Value]) -> Value {
    let safe: Vec<&Map<String, Value>> = events.iter().filter_map(Value::as_object).collect();
    let mut counts: BTreeMap<String, i64> = BTreeMap::new();
    for event in &safe {
        *counts
            .entry(text_or(event.get("event"), "unknown"))
            .or_default() += 1;
    }
    let count = |name: &str| counts.get(name).copied().unwrap_or(0);
    let positive: i64 = POSITIVE.iter().map(|n| count(n)).sum();
    let negative: i64 = NEGATIVE.iter().map(|n| count(n)).sum();
    let (mut source_events, mut source_positive, mut source_negative) = (0i64, 0i64, 0i64);
    for event in &safe {
        let source = event
            .get("metadata")
            .and_then(Value::as_object)
            .map(|m| text_or(m.get("source"), ""))
            .unwrap_or_default();
        if source != "model-suggestion" {
            continue;
        }
        let name = text_or(event.get("event"), "unknown");
        source_events += 1;
        if POSITIVE.contains(&name.as_str()) {
            source_positive += 1;
        }
        if NEGATIVE.contains(&name.as_str()) {
            source_negative += 1;
        }
    }
    json!({
        "event_count": safe.len(),
        "event_counts": counts,
        "positive_count": positive,
        "negative_count": negative,
        "regenerated_count": count("suggestion_regenerated"),
        "source_feedback": {
            "model-suggestion": {
                "event_count": source_events,
                "positive_count": source_positive,
                "negative_count": source_negative,
                "positive_rate": rate(source_positive, source_events),
                "negative_rate": rate(source_negative, source_events),
            }
        },
    })
}

/// `_adaptive_recommendation_items`.
fn recommendation_items(current: &Value, signals: &Map<String, Value>) -> Vec<Value> {
    let mut out: Vec<Value> = Vec::new();
    let profile = text_or(current.get("prompt_profile"), "conservative");
    let feedback = signals
        .get("feedback")
        .cloned()
        .unwrap_or_else(|| json!({}));
    let positive = optional_int(feedback.get("positive_count")).unwrap_or(0);
    let negative = optional_int(feedback.get("negative_count")).unwrap_or(0);
    let events = optional_int(feedback.get("event_count")).unwrap_or(0);
    let negative_dominates = negative >= positive.max(3);
    if negative_dominates {
        out.push(json!({
            "kind": "tune-style-before-slimming",
            "title": "Tune style before stronger prompt slimming",
            "detail": format!(
                "{negative} negative/regenerate event(s) versus {positive} positive event(s) \
                 suggest wording quality needs attention before cutting more evidence."
            ),
            "confidence": "medium",
            "suggested_config": {
                "suggestion_style": {
                    "guidance": "Describe the user-visible outcome first, then include the concrete changed area."
                }
            },
        }));
    }
    let int = |key: &str| optional_int(signals.get(key)).unwrap_or(0);
    let float = |key: &str| optional_float(signals.get(key)).unwrap_or(0.0);
    if int("prompt_eval_ms") >= 500 && float("prompt_eval_share") >= 0.35 {
        match profile.as_str() {
            "conservative" => out.push(json!({
                "kind": "use-lean-prompt-profile",
                "title": "Try the lean suggestion prompt profile",
                "detail": "Prompt evaluation dominates suggestion time; lean keeps core evidence while trimming prompt budget.",
                "confidence": if negative_dominates { "low" } else { "medium" },
                "suggested_config": {"suggestion_prompt_profile": "lean"},
            })),
            "lean" if !negative_dominates => out.push(json!({
                "kind": "use-minimal-prompt-profile",
                "title": "Consider the minimal suggestion prompt profile",
                "detail": "Lean prompts still spend most model time on prompt evaluation.",
                "confidence": if events < 3 { "low" } else { "medium" },
                "suggested_config": {"suggestion_prompt_profile": "minimal"},
            })),
            "minimal" => out.push(json!({
                "kind": "prompt-profile-at-minimum",
                "title": "Prompt profile is already minimal",
                "detail": "Prompt evaluation is still high, so the next lever is a faster model or server/runtime tuning.",
                "confidence": "medium",
            })),
            _ => {}
        }
    }
    let is_int = |value: Option<&Value>| value.and_then(Value::as_i64);
    if int("queue_ms") >= 250 && float("queue_share") >= 0.2 {
        let mut config = Map::new();
        if is_int(current.get("suggestion_parallelism")).is_some_and(|p| p > 1) {
            config.insert("suggestion_parallelism".into(), json!(1));
        }
        out.push(json!({
            "kind": "reduce-queueing",
            "title": "Reduce Ollama queueing",
            "detail": "Queue time suggests Ollama is serializing or overloaded; lower client parallelism or configure OLLAMA_NUM_PARALLEL before starting Ollama.",
            "confidence": "medium",
            "suggested_config": config,
        }));
    }
    if int("eval_ms") >= 500 && float("generation_share") >= 0.35 {
        let mut config =
            Map::from_iter([("provider".to_owned(), json!({"suggest_num_predict": 96}))]);
        if is_int(current.get("suggestion_count")).is_some_and(|c| c > 1) {
            config.insert("suggestion_count".into(), json!(1));
        }
        out.push(json!({
            "kind": "reduce-generation-cost",
            "title": "Reduce generation cost",
            "detail": "Generation dominates suggestion time; lower the suggestion output cap/count or use a faster suggestion model.",
            "confidence": "medium",
            "suggested_config": config,
        }));
    }
    if let Some(hit_rate) = numeric(signals.get("cache_hit_rate")) {
        if int("item_count") >= 2 && hit_rate < 0.5 {
            out.push(json!({
                "kind": "improve-cache-hit-rate",
                "title": "Improve suggestion cache hit rate",
                "detail": "Most visible suggestions were fresh model calls; keep VS Code background cache warming enabled for Ollama.",
                "confidence": "medium",
                "suggested_config": {"verlato.suggestionAutoWarmCache": true},
            }));
        }
    }
    if out.is_empty() {
        out.push(json!({
            "kind": "keep-current-settings",
            "title": "Keep current suggestion settings",
            "detail": "Timing and feedback signals do not show a strong reason to change profiles or request settings.",
            "confidence": "medium",
        }));
    }
    out
}

/// `_build_suggestion_adaptive_recommendations`.
pub fn build_recommendations(
    current: Value,
    timing_sources: &[Value],
    feedback_events: &[Value],
    cache_hit_count: i64,
    item_count: i64,
    prompt_chars: i64,
    source: &str,
) -> Value {
    let mut signals = Map::new();
    signals.insert("source".into(), json!(source));
    signals.insert("item_count".into(), json!(item_count));
    signals.insert("prompt_chars".into(), json!(prompt_chars));
    signals.insert(
        "average_prompt_chars".into(),
        if item_count == 0 {
            Value::Null
        } else {
            json!((prompt_chars as f64 / item_count as f64).round_ties_even() as i64)
        },
    );
    signals.insert("cache_hit_count".into(), json!(cache_hit_count));
    signals.insert(
        "cache_hit_rate".into(),
        if item_count == 0 {
            Value::Null
        } else {
            json!(round_to(cache_hit_count as f64 / item_count as f64, 3))
        },
    );
    signals.extend(timing_signals(timing_sources));
    signals.insert("feedback".into(), feedback_signals(feedback_events));
    let recommendations = recommendation_items(&current, &signals);
    json!({
        "schema_version": "verlato.suggestion_adaptive.v1",
        "current": current,
        "signals": signals,
        "recommendations": recommendations,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recommends_lean_profile_when_prompt_eval_dominates() {
        let current = current_settings("ollama", "conservative", 3, 2, 1, Some("single"), None);
        let sources = [
            json!({"timings": {"model_ms": 1000, "prompt_eval_ms": 700, "eval_ms": 200, "tokens_per_second": 30.25}}),
        ];
        let result = build_recommendations(current, &sources, &[], 0, 1, 900, "preview");
        assert_eq!(result["signals"]["prompt_eval_share"], json!(0.7));
        assert_eq!(
            result["signals"]["dominant_bottleneck"],
            json!("prompt_eval")
        );
        assert_eq!(result["signals"]["average_tokens_per_second"], json!(30.2));
        assert_eq!(
            result["recommendations"][0]["kind"],
            json!("use-lean-prompt-profile")
        );
    }

    #[test]
    fn keeps_settings_without_signals() {
        let current = current_settings("mock", "conservative", 1, 1, 1, None, None);
        let result = build_recommendations(current, &[], &[], 0, 0, 0, "preview");
        assert_eq!(result["signals"]["dominant_bottleneck"], json!("unknown"));
        assert_eq!(result["signals"]["cache_hit_rate"], Value::Null);
        assert_eq!(
            result["recommendations"][0]["kind"],
            json!("keep-current-settings")
        );
    }
}
