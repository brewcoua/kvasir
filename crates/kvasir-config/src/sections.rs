//! The stored sections of `Settings`, each with its defaults.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum LogFormat {
    /// For a log shipper: a run's lines interleave across tasks, so they must be parseable.
    Json,
    /// For reading by eye during development.
    Text,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LogSettings {
    pub level: String,
    pub format: LogFormat,
}

/// Models are `<provider>/<name>`, the provider being one named in the environment.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelSettings {
    pub fast: String,
    pub strong: String,
    /// The model for judgements. Unset falls back to `strong`.
    pub reason: Option<String>,
    pub embedding: String,
    /// A grounding checker such as MiniCheck, or none.
    pub checker: Option<String>,
    /// Sent only when set. Unset leaves the gateway's default, which for a thinking model served
    /// by Ollama spends each role's token budget on reasoning before any answer.
    pub reasoning_effort: Option<String>,
    /// Sent to the reason tier instead of `reasoning_effort`: that tier is meant to think.
    pub reason_effort: Option<String>,
    /// The context window the models are served with, such as Ollama's `num_ctx`.
    pub context_tokens: usize,
}

impl Default for ModelSettings {
    fn default() -> Self {
        Self {
            fast: String::new(),
            strong: String::new(),
            reason: None,
            embedding: String::new(),
            checker: None,
            reasoning_effort: None,
            reason_effort: Some("medium".into()),
            context_tokens: 16384,
        }
    }
}

/// A value `Debug` never prints and the settings API never returns.
#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(transparent)]
pub struct Secret(pub String);

impl std::fmt::Debug for Secret {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Secret(..)")
    }
}

/// A search provider reached with an API key.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct KeyedSettings {
    pub enabled: bool,
    pub api_key: Option<Secret>,
    /// In the fusion of the providers' results.
    pub weight: f64,
}

impl KeyedSettings {
    /// The key, when the provider is enabled and has one.
    pub fn usable_key(&self) -> Option<&str> {
        self.api_key.as_ref().filter(|_| self.enabled).map(|key| key.0.as_str())
    }
}

impl Default for KeyedSettings {
    fn default() -> Self {
        Self { enabled: false, api_key: None, weight: 1.0 }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SearxngSettings {
    pub enabled: bool,
    pub url: String,
    /// In the fusion of the providers' results.
    pub weight: f64,
}

impl Default for SearxngSettings {
    fn default() -> Self {
        Self { enabled: false, url: String::new(), weight: 1.0 }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SearchSettings {
    pub top_k: usize,
    pub searxng: SearxngSettings,
    pub brave: KeyedSettings,
    pub tavily: KeyedSettings,
    pub exa: KeyedSettings,
    pub mojeek: KeyedSettings,
}

impl SearchSettings {
    /// Whether a provider that searches the whole web can be asked.
    pub fn has_web_provider(&self) -> bool {
        (self.searxng.enabled && !self.searxng.url.is_empty())
            || [&self.brave, &self.tavily, &self.exa, &self.mojeek]
                .iter()
                .any(|provider| provider.usable_key().is_some())
    }
}

impl Default for SearchSettings {
    fn default() -> Self {
        Self {
            top_k: 3,
            searxng: SearxngSettings::default(),
            brave: KeyedSettings::default(),
            tavily: KeyedSettings::default(),
            exa: KeyedSettings::default(),
            mojeek: KeyedSettings::default(),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum RunStore {
    /// The default: a client such as Open WebUI keeps its own record of every answer.
    Memory,
    Sqlite,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RunSettings {
    pub store: RunStore,
    pub max: usize,
    pub ttl_days: u64,
    /// Off by default: prompts carry the pages read and the user's question, which some
    /// deployments must not retain. Timing and tokens are kept either way.
    pub keep_call_content: bool,
    pub max_concurrent: usize,
}

impl Default for RunSettings {
    fn default() -> Self {
        Self {
            store: RunStore::Memory,
            max: 100,
            ttl_days: 30,
            keep_call_content: false,
            max_concurrent: 1,
        }
    }
}

/// The deep research pipeline's defaults, which a request overrides one by one. Searches are the
/// budget: each costs about nine page reads.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ResearchSettings {
    pub max_searches: usize,
    pub results_per_query: usize,
    pub max_attempts: usize,
    pub follow_ups: bool,
    pub max_depth: usize,
    pub max_entities: usize,
    pub max_criteria: usize,
}

impl Default for ResearchSettings {
    fn default() -> Self {
        Self {
            max_searches: 16,
            results_per_query: 3,
            max_attempts: 4,
            follow_ups: true,
            max_depth: 2,
            max_entities: 8,
            max_criteria: 5,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StormSettings {
    pub max_conv_turn: usize,
    pub max_perspective: usize,
}

impl Default for StormSettings {
    fn default() -> Self {
        Self { max_conv_turn: 3, max_perspective: 3 }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Render {
    /// Render a page that needs JavaScript, when the browser is built in.
    Auto,
    Off,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FetchSettings {
    /// Named in the user agent so a site can reach whoever runs this. Empty names no one.
    pub contact: String,
    /// Between the starts of two requests to one host. 0 turns the wait off.
    pub host_interval_seconds: f64,
    pub render: Render,
    /// Hides the signs of automation. Getting past a challenge can break a site's terms, so it is
    /// the operator's call and off by default.
    pub stealth: bool,
    /// How long a cached page is served without asking its site. 0 fetches every page again.
    pub max_age_hours: u64,
}

impl Default for FetchSettings {
    fn default() -> Self {
        Self {
            contact: String::new(),
            host_interval_seconds: 1.0,
            render: Render::Auto,
            stealth: false,
            max_age_hours: 48,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum FaviconProvider {
    /// The site itself. The others learn each site looked up.
    Direct,
    Duckduckgo,
    Google,
    /// `favicons.custom_url`.
    Custom,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FaviconSettings {
    /// Tried in order, the first icon found wins. Empty turns favicons off.
    pub providers: Vec<FaviconProvider>,
    /// A URL with `{domain}` where the site's host goes.
    pub custom_url: String,
    pub ttl_days: u64,
    pub miss_ttl_days: u64,
}

impl Default for FaviconSettings {
    fn default() -> Self {
        Self {
            providers: vec![FaviconProvider::Direct],
            custom_url: String::new(),
            ttl_days: 30,
            miss_ttl_days: 3,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SourceSettings {
    /// Off by default: classifying a site asks Wikidata about the domains of search results.
    pub enabled: bool,
    pub wikidata: bool,
    pub endpoint: String,
    pub ttl_days: u64,
    pub miss_ttl_days: u64,
    /// Whether Wikipedia's perennial sources list tags hosts. Needs `enabled`.
    pub rsp: bool,
    pub allow: Vec<String>,
    pub deny: Vec<String>,
    /// Each `<label>=<url>`, the label a kind or a tag given to every host on the list.
    pub lists: Vec<String>,
}

impl Default for SourceSettings {
    fn default() -> Self {
        Self {
            enabled: false,
            wikidata: true,
            endpoint: "https://query.wikidata.org/sparql".into(),
            ttl_days: 30,
            miss_ttl_days: 7,
            rsp: false,
            allow: Vec::new(),
            deny: Vec::new(),
            lists: vec![
                "government=https://raw.githubusercontent.com/cisagov/dotgov-data/main/current-full.csv"
                    .into(),
            ],
        }
    }
}
