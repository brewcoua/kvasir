//! Which model, with what token budget, serves each step.
//!
//! The fast tier asks, extracts and plans; the strong tier writes what a reader sees; the reason
//! tier judges, and thinks before it answers, so its budgets are about three times what the answer
//! needs. STORM's budgets are upstream's own examples'.

use kvasir_config::{ConfigError, Settings};

use crate::Target;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tier {
    Fast,
    Strong,
    /// `model.reason`, or the strong model when that is unset.
    Reason,
}

/// The research steps' roles: name, tier and `max_tokens`. `intake` and `write` are sized from the
/// context instead; see [`research`].
pub const RESEARCH: &[(&str, Tier, u32)] = &[
    ("clarify", Tier::Fast, 500),
    // The strong model thinks before it answers too; a brief alone once used 876 of 1000.
    ("brief", Tier::Strong, 2500),
    ("decompose", Tier::Reason, 2000),
    ("plan", Tier::Fast, 500),
    ("check", Tier::Reason, 2500),
    ("criteria", Tier::Reason, 3000),
    ("entities", Tier::Reason, 2000),
    ("assess", Tier::Fast, 600),
    ("judge", Tier::Reason, 2500),
    // A thinking strong model spends some 700 tokens reasoning before it answers, within these.
    ("outline", Tier::Strong, 2500),
    ("tldr", Tier::Strong, 2000),
    ("verify", Tier::Fast, 600),
    ("contradict", Tier::Fast, 600),
    // The final say on a sentence the checker and the fast model reject or disagree on.
    ("escalate", Tier::Reason, 2500),
];

/// STORM's roles. Upstream puts polishing on the strong model; this deployment is billed per
/// token and prefers the cheaper tier there.
pub const STORM: &[(&str, Tier, u32)] = &[
    ("conv_simulator", Tier::Fast, 500),
    ("question_asker", Tier::Fast, 500),
    ("outline_gen", Tier::Strong, 400),
    ("article_gen", Tier::Strong, 700),
    ("article_polish", Tier::Fast, 4000),
];

/// Room for a handful of follow-up questions, the largest thing Open WebUI asks a task call for.
pub const TASK_MAX_TOKENS: u32 = 1000;

/// A bound on a section's prompt: one run's came to 2,200 to 2,700 tokens.
pub const WRITE_PROMPT_TOKENS: u32 = 4000;

/// A checker answers Yes or No.
pub const CHECKER_MAX_TOKENS: u32 = 10;

/// The model serving `tier`, for `role`. Every role but the checker samples at temperature 1.0
/// and top_p 0.9: STORM's perspectives depend on the simulated conversations diverging.
pub fn target(
    settings: &Settings,
    role: &'static str,
    tier: Tier,
    max_tokens: u32,
) -> Result<Target, ConfigError> {
    let model = &settings.model;
    let reference = match tier {
        Tier::Fast => &model.fast,
        Tier::Strong => &model.strong,
        Tier::Reason => model.reason.as_ref().filter(|r| !r.is_empty()).unwrap_or(&model.strong),
    };
    let effort = if tier == Tier::Reason { &model.reason_effort } else { &model.reasoning_effort };
    Ok(Target {
        model: settings.resolve(reference)?,
        role,
        max_tokens,
        temperature: 1.0,
        top_p: Some(0.9),
        reasoning_effort: effort.clone(),
    })
}

/// One target per research step, keyed by role, in [`RESEARCH`] order then `intake` and `write`.
pub fn research(settings: &Settings) -> Result<Vec<Target>, ConfigError> {
    let context = u32::try_from(settings.model.context_tokens).unwrap_or(u32::MAX);
    let mut targets = RESEARCH
        .iter()
        .map(|&(role, tier, max_tokens)| target(settings, role, tier, max_tokens))
        .collect::<Result<Vec<_>, _>>()?;
    // An intake call reads as much of a page as the context holds, so its answer grows with it.
    targets.push(target(settings, "intake", Tier::Fast, context / 4)?);
    // A section gets all the context its prompt leaves.
    let write = context.saturating_sub(WRITE_PROMPT_TOKENS).max(1);
    targets.push(target(settings, "write", Tier::Strong, write)?);
    Ok(targets)
}

/// The fast model, for a client's short housekeeping call that belongs to no run.
pub fn task(settings: &Settings) -> Result<Target, ConfigError> {
    target(settings, "task", Tier::Fast, TASK_MAX_TOKENS)
}

/// The grounding checker, if one is set: at temperature 0, since a verdict that changes between
/// two calls on the same quote is no check, and with no reasoning effort.
pub fn checker(settings: &Settings) -> Result<Option<Target>, ConfigError> {
    let Some(reference) = settings.model.checker.as_deref().filter(|r| !r.is_empty()) else {
        return Ok(None);
    };
    Ok(Some(Target {
        model: settings.resolve(reference)?,
        role: "ground",
        max_tokens: CHECKER_MAX_TOKENS,
        temperature: 0.0,
        top_p: None,
        reasoning_effort: None,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};

    fn settings(stored: Value) -> Settings {
        let env = [
            ("KVASIR_TOKEN".to_owned(), "t".to_owned()),
            ("KVASIR_SECRET_KEY".to_owned(), "ab".repeat(32)),
            ("KVASIR_PROVIDER_G_API_BASE".to_owned(), "http://g/v1".to_owned()),
        ];
        let Value::Object(stored) = stored else { panic!() };
        let mut warnings = Vec::new();
        let settings = Settings::from_env(env, &mut warnings).unwrap();
        let settings = settings.with_stored(&stored, true, &mut warnings).unwrap();
        assert!(warnings.is_empty(), "{warnings:?}");
        settings
    }

    fn by_role(targets: &[Target], role: &str) -> Target {
        targets.iter().find(|t| t.role == role).unwrap().clone()
    }

    #[test]
    fn tiers_efforts_and_budgets() {
        let s = settings(json!({
            "model.fast": "g/f", "model.strong": "g/s", "model.embedding": "g/e",
            "model.reasoning_effort": "none", "model.context_tokens": 16384,
        }));
        let targets = research(&s).unwrap();

        let decompose = by_role(&targets, "decompose");
        assert_eq!((decompose.model.name.as_str(), decompose.max_tokens), ("s", 2000));
        assert_eq!(decompose.reasoning_effort.as_deref(), Some("medium"));
        let plan = by_role(&targets, "plan");
        assert_eq!(
            (plan.model.name.as_str(), plan.reasoning_effort.as_deref()),
            ("f", Some("none"))
        );
        assert_eq!(by_role(&targets, "intake").max_tokens, 4096);
        assert_eq!(by_role(&targets, "write").max_tokens, 12384);
        assert_eq!(targets.len(), RESEARCH.len() + 2);
        assert_eq!(checker(&s).unwrap(), None);
    }

    #[test]
    fn the_reason_model_and_the_checker_when_set() {
        let s = settings(json!({
            "model.fast": "g/f", "model.strong": "g/s", "model.reason": "g/r",
            "model.checker": "g/c", "model.embedding": "g/e",
        }));

        assert_eq!(target(&s, "judge", Tier::Reason, 1).unwrap().model.name, "r");
        let checker = checker(&s).unwrap().unwrap();
        assert_eq!((checker.temperature, checker.top_p, checker.max_tokens), (0.0, None, 10));
    }
}
