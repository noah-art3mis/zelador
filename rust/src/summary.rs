//! The plan rendering `zel validate` prints (port of `apply.summarize`, which
//! `zel apply --dry-run` will share).

use std::collections::HashSet;

use crate::contracts::Plan;
use crate::pyjson::py_str;

/// Per intent group, what would be written.
pub fn summarize(plan: &Plan) -> Vec<String> {
    let mut lines = vec![format!(
        "plan {} — validated at library version {}, backup {}",
        plan.id,
        plan.library_version,
        py_str(&plan.backup)
    )];
    for (group, intent) in plan.intents.iter().enumerate() {
        let ops: Vec<_> = plan
            .operations
            .iter()
            .filter(|op| op.group == group)
            .collect();
        let high = ops.iter().filter(|op| op.risk == "high").count();
        let detail: Vec<String> = intent
            .as_object()
            .into_iter()
            .flatten()
            .filter(|(k, _)| *k != "op")
            .map(|(k, v)| format!("{k}={}", py_str(v)))
            .collect();
        let risk_note = if high > 0 {
            format!(", {high} high-risk")
        } else {
            String::new()
        };
        lines.push(format!(
            "  group {group} {}({}): {} operation(s){risk_note}",
            py_str(&intent["op"]),
            detail.join(", "),
            ops.len()
        ));
    }
    if let Some(settings) = &plan.settings {
        let count = settings["new"].as_array().map_or(0, Vec::len);
        lines.push(format!(
            "  settings: tagColors — {count} colour assignment(s)"
        ));
    }
    let objects: HashSet<(&str, &str)> = plan
        .operations
        .iter()
        .map(|op| (op.kind, op.key.as_str()))
        .collect();
    let writes = objects.len() + usize::from(plan.settings.is_some());
    lines.push(format!(
        "total: {} operation(s) on {} object(s), {writes} write request unit(s)",
        plan.operations.len(),
        objects.len()
    ));
    lines
}
