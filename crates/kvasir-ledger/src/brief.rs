//! What the brief reads, and choosing what a ranking or a table compares.

use std::collections::HashSet;

use kvasir_research::search::{Ranking, search};
use kvasir_research::text::names;
use kvasir_research::{ResearchError, Tools};
use kvasir_run::RunCtx;
use kvasir_run::stages::BACKGROUND;
use kvasir_types::requests::Clarification;
use kvasir_types::results::Criterion;
use kvasir_types::runs::Progress;

use crate::signatures::{criteria as choose_criteria, entities as choose_entities, plan_queries};

/// Searches whose snippets the criteria and the options are chosen from.
const BACKGROUND_QUERIES: usize = 3;

/// The criteria and the options to compare, chosen from search snippets where not given.
#[allow(clippy::too_many_arguments)]
pub async fn candidates(
    tools: &Tools,
    ctx: &RunCtx,
    today: &str,
    brief: &str,
    mut criteria: Vec<Criterion>,
    mut entities: Vec<String>,
    max_criteria: usize,
    max_entities: usize,
    per_query: usize,
) -> Result<(Vec<Criterion>, Vec<String>), ResearchError> {
    let publish = |detail: &str| ctx.publish(Progress::new(BACKGROUND, detail));
    if criteria.is_empty() || entities.is_empty() {
        publish("searching for the options and what sets them apart");
        let input = plan_queries::Input {
            today: today.to_owned(),
            brief: brief.to_owned(),
            notes: String::new(),
            count: BACKGROUND_QUERIES as i64,
        };
        let mut queries = tools.predict::<plan_queries::Sig>(ctx, "plan", &input).await?.queries;
        queries.truncate(BACKGROUND_QUERIES);
        // Snippets only, and not marked seen: the research still reads these pages.
        let hits = search(tools, ctx, &queries, per_query, &mut HashSet::new(), Ranking::default())
            .await?;
        let lines: Vec<String> =
            hits.iter().map(|hit| format!("{}: {}", hit.title, hit.description)).collect();
        let background = lines.join("\n");
        if criteria.is_empty() {
            publish("choosing the criteria");
            let input =
                choose_criteria::Input { brief: brief.to_owned(), background: background.clone() };
            let chosen =
                tools.predict::<choose_criteria::Sig>(ctx, "criteria", &input).await?.criteria;
            criteria = chosen
                .into_iter()
                .take(max_criteria)
                .map(|c| Criterion { name: c.name, weight: c.weight.clamp(1, 3) as u8 })
                .collect();
        }
        if entities.is_empty() {
            publish("choosing the options to compare");
            let input = choose_entities::Input {
                brief: brief.to_owned(),
                background: background.clone(),
                count: max_entities as i64,
            };
            let chosen =
                tools.predict::<choose_entities::Sig>(ctx, "entities", &input).await?.entities;
            // A small model fills a list with plausible names, such as model sizes never released;
            // searching for those finds nothing. Only options the results mention are kept.
            entities =
                chosen.into_iter().filter(|e| names(e, &background)).take(max_entities).collect();
        }
    }
    // The first spelling of a name given twice wins.
    let mut keys = HashSet::new();
    criteria.retain(|c| !key(&c.name).is_empty() && keys.insert(key(&c.name)));
    let mut keys = HashSet::new();
    entities.retain(|e| !key(e).is_empty() && keys.insert(key(e)));
    let entities: Vec<String> = entities.into_iter().map(|e| e.trim().to_owned()).collect();
    publish(&format!(
        "comparing {} options on {} criteria: {}",
        entities.len(),
        criteria.len(),
        entities.join(", ")
    ));
    Ok((criteria, entities))
}

pub fn clarifications(clarifications: &[Clarification]) -> String {
    if clarifications.is_empty() {
        return "none".into();
    }
    let lines: Vec<String> =
        clarifications.iter().map(|c| format!("Q: {}\nA: {}", c.question, c.answer)).collect();
    lines.join("\n")
}

fn key(name: &str) -> String {
    name.trim().to_lowercase()
}
