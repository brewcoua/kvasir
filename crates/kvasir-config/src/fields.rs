//! Every stored field: its key, how its JSON value is checked, and what the settings page shows.

use serde_json::Value;

use crate::ConfigError;
use crate::repr::repr;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    /// A whole number of at least `min`.
    Int {
        min: i64,
    },
    /// A finite number, 0 or more.
    Float,
    Bool,
    /// Text, trimmed. `choices`, when not empty, are the values allowed after lowercasing.
    Text {
        choices: &'static [&'static str],
    },
    /// Text that may be unset: `null` or `""`.
    OptionalText,
    /// Text that is never shown: not by the API, in `Debug`, or in a message about a bad value.
    Secret,
    /// A list of trimmed, non-empty strings, each one of `choices` after lowercasing when given.
    List {
        choices: &'static [&'static str],
    },
}

#[derive(Debug)]
pub struct Field {
    /// `<section>.<name>`, as stored. The name may hold dots, for a struct within the section.
    pub key: &'static str,
    pub kind: Kind,
    /// What the settings page shows under the field.
    pub help: &'static str,
    /// The service cannot run anything until it is set.
    pub required: bool,
    /// Read once at startup, so a change applies at the next.
    pub restart: bool,
}

impl Field {
    pub fn section(&self) -> &'static str {
        self.key.split_once('.').expect("keys hold a section").0
    }

    pub fn name(&self) -> &'static str {
        self.key.split_once('.').expect("keys hold a section").1
    }

    /// Whether the field names a model, as `<provider>/<name>`.
    pub fn is_model(&self) -> bool {
        MODEL_KEYS.contains(&self.key)
    }

    /// `value` checked and normalized, as the section struct reads it.
    pub(crate) fn parse(&self, value: &Value) -> Result<Value, ConfigError> {
        let key = self.key;
        let bad =
            |what: &str| ConfigError::new(format!("{key} must be {what}, got {}", repr(value)));
        match self.kind {
            Kind::Int { min } => {
                if !value.is_i64() && !value.is_u64() {
                    return Err(bad("an integer"));
                }
                if let Some(n) = value.as_i64().filter(|n| *n < min) {
                    return Err(ConfigError::new(format!("{key} must be at least {min}, got {n}")));
                }
                Ok(value.clone())
            }
            Kind::Float => {
                let Some(f) = value.as_f64() else { return Err(bad("a number")) };
                if !f.is_finite() || f < 0.0 {
                    return Err(bad("a finite number, 0 or more"));
                }
                Ok(f.into())
            }
            Kind::Bool if value.is_boolean() => Ok(value.clone()),
            Kind::Bool => Err(bad("true or false")),
            Kind::List { choices } => {
                let items: Option<Vec<&str>> =
                    value.as_array().and_then(|items| items.iter().map(Value::as_str).collect());
                let Some(items) = items else { return Err(bad("a list of strings")) };
                let mut items: Vec<String> = items
                    .iter()
                    .map(|s| s.trim())
                    .filter(|s| !s.is_empty())
                    .map(Into::into)
                    .collect();
                if choices.is_empty() {
                    return Ok(items.into());
                }
                items.iter_mut().for_each(|s| *s = s.to_lowercase());
                let unknown: Vec<String> = items
                    .iter()
                    .filter(|s| !choices.contains(&s.as_str()))
                    .map(|s| repr(&Value::from(s.as_str())))
                    .collect();
                if !unknown.is_empty() {
                    return Err(ConfigError::new(format!(
                        "{key} holds {}; each must be one of {}",
                        unknown.join(", "),
                        choices.join(", ")
                    )));
                }
                Ok(items.into())
            }
            Kind::Secret => match value.as_str().map(str::trim) {
                Some(text) if !text.is_empty() => Ok(text.into()),
                _ => Err(ConfigError::new(format!("{key} must be a non-empty string"))),
            },
            Kind::OptionalText if value.is_null() || value == "" => Ok(Value::Null),
            Kind::OptionalText | Kind::Text { .. } => {
                let Some(text) = value.as_str() else { return Err(bad("a string")) };
                let mut text = text.trim().to_owned();
                if key == "search.searxng.url" {
                    text.truncate(text.trim_end_matches('/').len());
                }
                if let Kind::Text { choices } = self.kind {
                    choose(key, &mut text, choices)?;
                }
                Ok(text.into())
            }
        }
    }
}

/// Lowercases `text` and checks it against `choices`, when there are any.
pub(crate) fn choose(name: &str, text: &mut String, choices: &[&str]) -> Result<(), ConfigError> {
    if choices.is_empty() {
        return Ok(());
    }
    *text = text.to_lowercase();
    if choices.contains(&text.as_str()) {
        return Ok(());
    }
    let got = repr(&Value::from(text.as_str()));
    Err(ConfigError::new(format!("{name} must be {}, got {got}", choices.join(" or "))))
}

/// The stored fields that name a model, in the order they are checked.
pub const MODEL_KEYS: [&str; 5] =
    ["model.fast", "model.strong", "model.reason", "model.embedding", "model.checker"];

const fn field(key: &'static str, kind: Kind, help: &'static str) -> Field {
    Field { key, kind, help, required: false, restart: false }
}

const fn required(key: &'static str, help: &'static str) -> Field {
    Field { key, kind: TEXT, help, required: true, restart: false }
}

const fn restart(key: &'static str, kind: Kind, help: &'static str) -> Field {
    Field { key, kind, help, required: false, restart: true }
}

const INT: Kind = Kind::Int { min: 1 };
const TEXT: Kind = Kind::Text { choices: &[] };

/// Every stored field, by section, in the order Python declared them.
pub const FIELDS: &[Field] = &[
    required(
        "model.fast",
        "Conversation simulation, question asking, polishing, and research intake.",
    ),
    required(
        "model.strong",
        "Outline and article generation, and the research outline, report and summary.",
    ),
    field(
        "model.reason",
        Kind::OptionalText,
        "Research judgements, and the last word on a doubtful sentence. Unset uses the strong \
         model.",
    ),
    required("model.embedding", "Embedding model, for both modes."),
    field(
        "model.checker",
        Kind::OptionalText,
        "A grounding checker such as MiniCheck. Unset leaves every check to the fast model.",
    ),
    field(
        "model.reasoning_effort",
        Kind::OptionalText,
        "Sent as reasoning_effort with every call but the reason model's. none stops a thinking \
         model on Ollama spending its budget on reasoning.",
    ),
    field(
        "model.reason_effort",
        Kind::OptionalText,
        "Sent as reasoning_effort to the reason model.",
    ),
    field(
        "model.context_tokens",
        INT,
        "The context window the models are served with, such as Ollama's num_ctx, not the model's \
         maximum.",
    ),
    field("search.top_k", INT, "Search results per query."),
    field("search.searxng.enabled", Kind::Bool, "Search through a SearXNG instance."),
    field("search.searxng.url", TEXT, "SearXNG base URL."),
    field("search.searxng.weight", Kind::Float, "Its weight when results are fused."),
    field("search.brave.enabled", Kind::Bool, "Search through the Brave Search API."),
    field("search.brave.api_key", Kind::Secret, "Brave Search API key."),
    field("search.brave.weight", Kind::Float, "Its weight when results are fused."),
    field("search.tavily.enabled", Kind::Bool, "Search through the Tavily API."),
    field("search.tavily.api_key", Kind::Secret, "Tavily API key."),
    field("search.tavily.weight", Kind::Float, "Its weight when results are fused."),
    field("search.exa.enabled", Kind::Bool, "Search through the Exa API."),
    field("search.exa.api_key", Kind::Secret, "Exa API key."),
    field("search.exa.weight", Kind::Float, "Its weight when results are fused."),
    field("search.mojeek.enabled", Kind::Bool, "Search through the Mojeek API."),
    field("search.mojeek.api_key", Kind::Secret, "Mojeek API key."),
    field("search.mojeek.weight", Kind::Float, "Its weight when results are fused."),
    field("search.wikipedia.enabled", Kind::Bool, "Search English Wikipedia."),
    field("search.wikipedia.weight", Kind::Float, "Its weight when results are fused."),
    field("search.arxiv.enabled", Kind::Bool, "Search arXiv's preprints."),
    field("search.arxiv.weight", Kind::Float, "Its weight when results are fused."),
    restart(
        "runs.store",
        Kind::Text { choices: &["memory", "sqlite"] },
        "sqlite keeps runs across restarts.",
    ),
    restart("runs.max", INT, "Runs kept, most recent first."),
    restart("runs.ttl_days", INT, "Age past which a stored run is deleted. sqlite only."),
    restart(
        "runs.keep_call_content",
        Kind::Bool,
        "Keep each model call's messages and answer, and each search's results, with its run.",
    ),
    field("runs.max_concurrent", INT, "Concurrent runs. Beyond this, a request gets 429."),
    field("research.max_searches", INT, "Searches in a run, before the 8 for empty cells."),
    field("research.results_per_query", INT, "Pages read per search by the ledger."),
    field("research.max_attempts", INT, "Queries tried on one question before it is given up."),
    field("research.follow_ups", Kind::Bool, "Whether an answered question can add harder ones."),
    field("research.max_depth", INT, "Levels of follow-ups below the first questions."),
    field(
        "research.max_entities",
        INT,
        "Options a ranking or table compares, when the run chooses them.",
    ),
    field("research.max_criteria", INT, "Criteria, when the run chooses them."),
    field("storm.max_conv_turn", INT, "Questions per perspective."),
    field("storm.max_perspective", INT, "Perspectives to research."),
    field("fetch.contact", TEXT, "Contact named in the user agent of page requests."),
    field(
        "fetch.host_interval_seconds",
        Kind::Float,
        "Least time between the starts of two page requests to one host.",
    ),
    field(
        "fetch.render",
        Kind::Text { choices: &["auto", "off"] },
        "auto renders a page that needs JavaScript, in a build with Chromium: the image's browser \
         target.",
    ),
    field(
        "fetch.stealth",
        Kind::Bool,
        "Start Chromium without the launch flags that name automation. Getting past a challenge \
         can break a site's terms.",
    ),
    field(
        "fetch.max_age_hours",
        Kind::Int { min: 0 },
        "Hours a cached page is served without asking its site. 0 fetches every page again.",
    ),
    field(
        "favicons.providers",
        Kind::List { choices: &["direct", "duckduckgo", "google", "custom"] },
        "Where site icons come from, tried in order. Empty turns favicons off. All but direct \
         learn each site looked up.",
    ),
    field(
        "favicons.custom_url",
        TEXT,
        "URL for the custom provider, with {domain} where the site's host goes.",
    ),
    field("favicons.ttl_days", INT, "Days a found icon is served before it is looked up again."),
    field("favicons.miss_ttl_days", INT, "Days a site with no icon is remembered."),
    field(
        "sources.enabled",
        Kind::Bool,
        "Classify the sites of search results by kind and trust tier.",
    ),
    field(
        "sources.wikidata",
        Kind::Bool,
        "Ask Wikidata about hosts no rule, mark or list decides. Off sends no domain anywhere.",
    ),
    field("sources.endpoint", TEXT, "SPARQL endpoint asked."),
    field(
        "sources.ttl_days",
        INT,
        "Days a Wikidata answer is kept, and a downloaded list served before it is fetched again.",
    ),
    field("sources.miss_ttl_days", INT, "Days a host Wikidata does not know is remembered."),
    field("sources.rsp", Kind::Bool, "Also tag hosts by Wikipedia's perennial sources list."),
    field(
        "sources.allow",
        Kind::List { choices: &[] },
        "Hosts, with their subdomains, always trusted. Beats any mark.",
    ),
    field(
        "sources.deny",
        Kind::List { choices: &[] },
        "Hosts, with their subdomains, never read. Beats sources.allow and any mark.",
    ),
    field(
        "sources.lists",
        Kind::List { choices: &[] },
        "Lists to download, each as <label>=<url>, the label a kind or a tag.",
    ),
];

pub fn field_for(key: &str) -> Option<&'static Field> {
    FIELDS.iter().find(|f| f.key == key)
}
