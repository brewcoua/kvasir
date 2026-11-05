//! kvasir's settings, from the environment and the `settings` table of `kvasir.db`.
//!
//! Two sources, each setting in exactly one. The environment holds what a deployment fixes and
//! what is secret: the token, the data directory, the address, thread width, logging and the model
//! providers. Everything else is stored and edited through the API; a field with nothing stored has
//! its default.
//!
//! An environment variable is `KVASIR_<KEY>` for a top-level field and `KVASIR_LOG_<KEY>` for
//! logging. A provider is `KVASIR_PROVIDER_<NAME>_API_BASE` and `KVASIR_PROVIDER_<NAME>_API_KEY`. A
//! stored field is keyed by its path, `<section>.<key>`, so `research.max_searches`.
//!
//! A bad environment stops the start. A bad stored value is reported in `warnings` and its default
//! used instead, since the API that would fix it must still come up. `strict` fails instead, for
//! checking a change before it is stored. The models and the search URL have no default: until they
//! are stored the service runs, and [`Settings::missing`] names them.
//!
//! The messages are the ones Python wrote, values quoted as Python's `repr` quotes them, so
//! `tests/golden/settings.json` can hold this crate to them.

mod fields;
mod repr;
pub mod store;

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use kvasir_types::sites::{KINDS, LIST_TAGS, is_list_label, normalize_host};
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

pub use fields::{FIELDS, Field, Kind, MODEL_KEYS, field_for};
use repr::text as quote;

/// Sent when a provider has no key: the OpenAI client refuses an empty one, and Ollama ignores it.
pub const NO_API_KEY: &str = "none";

const PROVIDER_PREFIX: &str = "KVASIR_PROVIDER_";
const ENV_NAMES: [&str; 7] = [
    "KVASIR_DATA_DIR",
    "KVASIR_TOKEN",
    "KVASIR_HOST",
    "KVASIR_PORT",
    "KVASIR_MAX_THREADS",
    "KVASIR_LOG_LEVEL",
    "KVASIR_LOG_FORMAT",
];

/// Configuration is missing or unusable.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct ConfigError(String);

impl ConfigError {
    pub(crate) fn new(message: impl Into<String>) -> Self {
        Self(message.into())
    }
}

/// Something that calls a model or searches, asked for before the settings it needs.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{}", .0.join(", "))]
pub struct SetupRequired(pub Vec<&'static str>);

/// An OpenAI-compatible endpoint models are served from.
#[derive(Clone, PartialEq, Eq)]
pub struct Provider {
    pub api_base: String,
    pub api_key: String,
}

impl std::fmt::Debug for Provider {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Provider").field("api_base", &self.api_base).finish_non_exhaustive()
    }
}

/// A model reference resolved against its provider.
#[derive(Clone, PartialEq, Eq)]
pub struct Model {
    /// The name the provider knows the model by.
    pub name: String,
    pub api_base: String,
    pub api_key: String,
}

impl std::fmt::Debug for Model {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Model")
            .field("name", &self.name)
            .field("api_base", &self.api_base)
            .finish_non_exhaustive()
    }
}

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

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SearchSettings {
    pub url: String,
    pub top_k: usize,
}

impl Default for SearchSettings {
    fn default() -> Self {
        Self { url: String::new(), top_k: 3 }
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

/// Resolved configuration: [`Settings::from_env`], then [`Settings::with_stored`].
#[derive(Clone, PartialEq)]
pub struct Settings {
    pub data_dir: PathBuf,
    /// Every route but health, the login and the page needs it.
    pub token: String,
    pub host: String,
    pub port: u16,
    /// How wide fan-out runs, and how many search or page requests may be in flight at once.
    pub max_threads: usize,
    pub providers: BTreeMap<String, Provider>,
    pub log: LogSettings,
    pub model: ModelSettings,
    pub search: SearchSettings,
    pub runs: RunSettings,
    pub research: ResearchSettings,
    pub storm: StormSettings,
    pub fetch: FetchSettings,
    pub favicons: FaviconSettings,
    pub sources: SourceSettings,
}

impl std::fmt::Debug for Settings {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Settings")
            .field("data_dir", &self.data_dir)
            .field("host", &self.host)
            .field("port", &self.port)
            .field("max_threads", &self.max_threads)
            .field("providers", &self.providers)
            .field("log", &self.log)
            .field("model", &self.model)
            .field("search", &self.search)
            .field("runs", &self.runs)
            .field("research", &self.research)
            .field("storm", &self.storm)
            .field("fetch", &self.fetch)
            .field("favicons", &self.favicons)
            .field("sources", &self.sources)
            .finish_non_exhaustive()
    }
}

impl Settings {
    /// The environment's settings, with every stored field at its default. Only `KVASIR_*`
    /// variables are read, trimmed, and an empty one is unset. A variable that names no setting is
    /// reported in `warnings`.
    pub fn from_env(
        env: impl IntoIterator<Item = (String, String)>,
        warnings: &mut Vec<String>,
    ) -> Result<Self, ConfigError> {
        let env: Vec<(String, String)> = env
            .into_iter()
            .filter_map(|(k, v)| {
                let v = v.trim();
                (k.starts_with("KVASIR_") && !v.is_empty()).then(|| (k, v.to_owned()))
            })
            .collect();
        let get = |name: &str| env.iter().rev().find(|(k, _)| k == name).map(|(_, v)| v.as_str());

        let raw = raw_providers(&env)?;
        let mut missing: Vec<String> = raw
            .iter()
            .filter(|(_, [base, _])| base.is_none())
            .map(|(name, _)| format!("{PROVIDER_PREFIX}{}_API_BASE", name.to_uppercase()))
            .collect();
        if get("KVASIR_TOKEN").is_none() {
            missing.push("KVASIR_TOKEN".into());
        }
        if !missing.is_empty() {
            return Err(ConfigError::new(format!(
                "missing required environment variables: {}",
                missing.join(", ")
            )));
        }
        let mut unread: Vec<&str> = env
            .iter()
            .map(|(k, _)| k.as_str())
            .filter(|k| !ENV_NAMES.contains(k) && !k.starts_with(PROVIDER_PREFIX))
            .collect();
        unread.sort_unstable();
        unread.dedup();
        if !unread.is_empty() {
            warnings.push(format!(
                "not read from the environment, set them in the settings instead: {}",
                unread.join(", ")
            ));
        }

        let int = |name: &str, default: i64| -> Result<i64, ConfigError> {
            let Some(raw) = get(name) else { return Ok(default) };
            let value = python_int(raw).ok_or_else(|| {
                ConfigError::new(format!("{name} must be an integer, got {}", quote(raw)))
            })?;
            if value < 1 {
                return Err(ConfigError::new(format!("{name} must be at least 1, got {value}")));
            }
            Ok(value)
        };
        let data_dir = get("KVASIR_DATA_DIR").map_or_else(
            || PathBuf::from("/data"),
            // As Python's Path: no trailing slash, no doubled separators or `.` parts.
            |raw| Path::new(raw).components().collect(),
        );
        let token = get("KVASIR_TOKEN").expect("checked above").to_owned();
        let host = get("KVASIR_HOST").unwrap_or("0.0.0.0").to_owned();
        let port = int("KVASIR_PORT", 8080)?;
        let port = u16::try_from(port).map_err(|_| {
            ConfigError::new(format!("KVASIR_PORT must be at most 65535, got {port}"))
        })?;
        let max_threads = int("KVASIR_MAX_THREADS", 10)?;
        let max_threads = usize::try_from(max_threads).expect("at least 1");
        let level = get("KVASIR_LOG_LEVEL").unwrap_or("INFO").to_uppercase();
        let mut format = get("KVASIR_LOG_FORMAT").unwrap_or("json").to_owned();
        fields::choose("KVASIR_LOG_FORMAT", &mut format, &["json", "text"])?;
        let format = if format == "json" { LogFormat::Json } else { LogFormat::Text };

        let providers = raw
            .into_iter()
            .map(|(name, [base, key])| {
                let base = base.expect("checked above");
                let api_base = base.trim_end_matches('/').to_owned();
                (name, Provider { api_base, api_key: key.unwrap_or_else(|| NO_API_KEY.into()) })
            })
            .collect();
        Ok(Self {
            data_dir,
            token,
            host,
            port,
            max_threads,
            providers,
            log: LogSettings { level, format },
            model: ModelSettings::default(),
            search: SearchSettings::default(),
            runs: RunSettings::default(),
            research: ResearchSettings::default(),
            storm: StormSettings::default(),
            fetch: FetchSettings::default(),
            favicons: FaviconSettings::default(),
            sources: SourceSettings::default(),
        })
    }

    /// These settings' environment with `stored` in place of every stored field. A field not in
    /// `stored`, or whose value is unusable, has its default.
    pub fn with_stored(
        &self,
        stored: &Map<String, Value>,
        strict: bool,
        warnings: &mut Vec<String>,
    ) -> Result<Self, ConfigError> {
        let mut problem = |message: String| problem(message, strict, warnings);
        let mut unknown: Vec<&String> = stored.keys().filter(|k| field_for(k).is_none()).collect();
        unknown.sort_unstable();
        for key in unknown {
            problem(format!("unknown setting {key}"))?;
        }
        let mut parsed: BTreeMap<&str, Map<String, Value>> = BTreeMap::new();
        for field in FIELDS {
            let Some(value) = stored.get(field.key) else { continue };
            match field.parse(value) {
                Ok(value) => {
                    parsed.entry(field.section()).or_default().insert(field.name().into(), value);
                }
                Err(err) => problem(err.0)?,
            }
        }
        // A provider dropped from the environment leaves its models unset rather than failing the
        // first run that uses one.
        if let Some(model) = parsed.get_mut("model") {
            for key in MODEL_KEYS {
                let name = &key["model.".len()..];
                let Some(reference) = model.get(name).and_then(Value::as_str) else { continue };
                if reference.is_empty() {
                    continue;
                }
                if let Err(err) = self.resolve(reference) {
                    problem(format!("{key}: {err}"))?;
                    model.remove(name);
                }
            }
        }

        let mut settings = self.clone();
        settings.model = section(&mut parsed, "model");
        settings.search = section(&mut parsed, "search");
        settings.runs = section(&mut parsed, "runs");
        settings.research = section(&mut parsed, "research");
        settings.storm = section(&mut parsed, "storm");
        settings.fetch = section(&mut parsed, "fetch");
        settings.favicons = section(&mut parsed, "favicons");
        settings.sources = section(&mut parsed, "sources");
        if let Err(err) = check_favicons(&settings.favicons) {
            problem(err.0)?;
            settings.favicons = FaviconSettings::default();
        }
        if let Err(err) = check_sources(&settings.sources) {
            problem(err.0)?;
            settings.sources = SourceSettings::default();
        }
        Ok(settings)
    }

    /// The stored field `key`, such as `research.max_searches`, as JSON.
    pub fn value(&self, key: &str) -> Option<Value> {
        let (section, name) = key.split_once('.')?;
        let value = match section {
            "model" => serde_json::to_value(&self.model),
            "search" => serde_json::to_value(&self.search),
            "runs" => serde_json::to_value(&self.runs),
            "research" => serde_json::to_value(&self.research),
            "storm" => serde_json::to_value(&self.storm),
            "fetch" => serde_json::to_value(&self.fetch),
            "favicons" => serde_json::to_value(&self.favicons),
            "sources" => serde_json::to_value(&self.sources),
            _ => return None,
        };
        value.expect("sections serialize").get(name).cloned()
    }

    /// The stored fields the service needs before it can run anything, still unset.
    pub fn missing(&self) -> Vec<&'static str> {
        FIELDS
            .iter()
            .filter(|f| f.required && self.value(f.key).is_none_or(|v| v == ""))
            .map(|f| f.key)
            .collect()
    }

    pub fn ready(&self) -> Result<(), SetupRequired> {
        let missing = self.missing();
        if missing.is_empty() { Ok(()) } else { Err(SetupRequired(missing)) }
    }

    /// A `<provider>/<name>` reference as the model to call. Only the first `/` splits, since the
    /// name may hold more: `local/deepseek-v4.1-flash:cloud`, `cloud/org/model:tag`.
    pub fn resolve(&self, reference: &str) -> Result<Model, ConfigError> {
        let r = quote(reference);
        let Some((provider, name)) = reference.split_once('/').filter(|(_, name)| !name.is_empty())
        else {
            return Err(ConfigError::new(format!(
                "model {r} must name a provider, as in <provider>/<name>"
            )));
        };
        let Some(endpoint) = self.providers.get(provider) else {
            let configured: Vec<&str> = self.providers.keys().map(String::as_str).collect();
            let configured =
                if configured.is_empty() { "none".into() } else { configured.join(", ") };
            return Err(ConfigError::new(format!(
                "model {r} names unknown provider {}; configured: {configured}",
                quote(provider)
            )));
        };
        Ok(Model {
            name: name.into(),
            api_base: endpoint.api_base.clone(),
            api_key: endpoint.api_key.clone(),
        })
    }

    /// The stored settings, and the runs when `runs.store` is sqlite.
    pub fn db(&self) -> PathBuf {
        self.data_dir.join("kvasir.db")
    }

    pub fn pages_db(&self) -> PathBuf {
        self.data_dir.join("pages.db")
    }

    pub fn favicons_db(&self) -> PathBuf {
        self.data_dir.join("favicons.db")
    }

    pub fn sources_db(&self) -> PathBuf {
        self.data_dir.join("sources.db")
    }
}

/// Stored rows, each a field's JSON text, as values for [`Settings::with_stored`]. A row that is
/// not JSON is a problem like any bad value.
pub fn decode(
    rows: Vec<(String, String)>,
    strict: bool,
    warnings: &mut Vec<String>,
) -> Result<Map<String, Value>, ConfigError> {
    let mut stored = Map::new();
    for (key, text) in rows {
        match serde_json::from_str(&text) {
            Ok(value) => {
                stored.insert(key, value);
            }
            Err(_) => problem(format!("{key} is not JSON: {}", quote(&text)), strict, warnings)?,
        }
    }
    Ok(stored)
}

/// A stored value that cannot be used: an error when `strict`, otherwise a warning.
fn problem(message: String, strict: bool, warnings: &mut Vec<String>) -> Result<(), ConfigError> {
    if strict {
        return Err(ConfigError(message));
    }
    warnings.push(format!("stored setting ignored: {message}"));
    Ok(())
}

/// `T`'s defaults with the values parsed for it in place.
fn section<T: Default + Serialize + DeserializeOwned>(
    parsed: &mut BTreeMap<&str, Map<String, Value>>,
    name: &str,
) -> T {
    let Some(values) = parsed.remove(name) else { return T::default() };
    let mut section = serde_json::to_value(T::default()).expect("sections serialize");
    section.as_object_mut().expect("sections are objects").extend(values);
    serde_json::from_value(section).expect("a checked value fits its section")
}

/// A provider's name, with its base and key as the environment gives them.
type RawProvider = (String, [Option<String>; 2]);

/// The providers `env` names, in the order first seen, each with its base and key. Parsed by
/// fixed suffix, so a provider name may itself contain `_`.
fn raw_providers(env: &[(String, String)]) -> Result<Vec<RawProvider>, ConfigError> {
    let mut found: Vec<RawProvider> = Vec::new();
    for (name, value) in env {
        for (i, suffix) in ["_API_BASE", "_API_KEY"].into_iter().enumerate() {
            if !name.starts_with(PROVIDER_PREFIX) || !name.ends_with(suffix) {
                continue;
            }
            let provider = name
                .get(PROVIDER_PREFIX.len()..name.len() - suffix.len())
                .unwrap_or("")
                .to_lowercase();
            let valid = !provider.is_empty()
                && provider.bytes().all(|b| matches!(b, b'a'..=b'z' | b'0'..=b'9' | b'_' | b'-'));
            if !valid {
                return Err(ConfigError::new(format!(
                    "{name} does not name a provider: names are lowercase letters, digits, _ and -"
                )));
            }
            match found.iter_mut().find(|(p, _)| *p == provider) {
                Some((_, values)) => values[i] = Some(value.clone()),
                None => {
                    let mut values = [None, None];
                    values[i] = Some(value.clone());
                    found.push((provider, values));
                }
            }
        }
    }
    Ok(found)
}

/// An integer as Python's `int(text)` reads one: a sign, and `_` between digits.
fn python_int(text: &str) -> Option<i64> {
    let (negative, digits) = match text.as_bytes().first()? {
        b'-' => (true, &text[1..]),
        b'+' => (false, &text[1..]),
        _ => (false, text),
    };
    let groups_ok =
        digits.split('_').all(|g| !g.is_empty() && g.bytes().all(|b| b.is_ascii_digit()));
    if !groups_ok {
        return None;
    }
    let n: i64 = digits.replace('_', "").parse().ok()?;
    Some(if negative { -n } else { n })
}

/// As Python's `urlsplit` sees it: an http or https scheme and a non-empty network location.
fn is_http_url(url: &str) -> bool {
    let Some((scheme, rest)) = url.split_once(':') else { return false };
    let netloc = rest.strip_prefix("//").map(|r| r.split(['/', '?', '#']).next().unwrap_or(""));
    (scheme.eq_ignore_ascii_case("http") || scheme.eq_ignore_ascii_case("https"))
        && netloc.is_some_and(|n| !n.is_empty())
}

fn check_favicons(favicons: &FaviconSettings) -> Result<(), ConfigError> {
    if !favicons.providers.contains(&FaviconProvider::Custom) {
        return Ok(());
    }
    let url = &favicons.custom_url;
    if url.contains("{domain}") && is_http_url(&url.replace("{domain}", "example.com")) {
        return Ok(());
    }
    Err(ConfigError::new(format!(
        "favicons.custom_url must be an http(s) URL containing {{domain}} when the custom provider \
         is used, got {}",
        quote(url)
    )))
}

fn check_sources(sources: &SourceSettings) -> Result<(), ConfigError> {
    if !sources.enabled {
        return Ok(());
    }
    let check_url = |name: &str, url: &str| {
        if is_http_url(url) {
            Ok(())
        } else {
            Err(ConfigError::new(format!(
                "sources.{name} must be an http(s) URL, got {}",
                quote(url)
            )))
        }
    };
    check_url("endpoint", &sources.endpoint)?;
    for entry in &sources.lists {
        let (label, url) = entry.split_once('=').unwrap_or((entry, ""));
        if !is_list_label(label) {
            let mut labels: Vec<&str> =
                KINDS[..KINDS.len() - 1].iter().chain(&LIST_TAGS).copied().collect();
            labels.sort_unstable();
            return Err(ConfigError::new(format!(
                "sources.lists entry {} must be <label>=<url> with a label of {}",
                quote(entry),
                labels.join(", ")
            )));
        }
        check_url("lists", url)?;
    }
    for (name, hosts) in [("allow", &sources.allow), ("deny", &sources.deny)] {
        for host in hosts {
            if normalize_host(host.strip_suffix('.').unwrap_or(host)).is_none() {
                return Err(ConfigError::new(format!(
                    "sources.{name} holds {}, which is not a DNS name",
                    quote(host)
                )));
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_field_table_matches_the_sections() {
        let settings =
            Settings::from_env([("KVASIR_TOKEN".into(), "t".into())], &mut vec![]).unwrap();
        let mut keys = Vec::new();
        for (name, value) in [
            ("model", serde_json::to_value(&settings.model)),
            ("search", serde_json::to_value(&settings.search)),
            ("runs", serde_json::to_value(&settings.runs)),
            ("research", serde_json::to_value(&settings.research)),
            ("storm", serde_json::to_value(&settings.storm)),
            ("fetch", serde_json::to_value(&settings.fetch)),
            ("favicons", serde_json::to_value(&settings.favicons)),
            ("sources", serde_json::to_value(&settings.sources)),
        ] {
            keys.extend(value.unwrap().as_object().unwrap().keys().map(|k| format!("{name}.{k}")));
        }
        assert_eq!(keys, FIELDS.iter().map(|f| f.key).collect::<Vec<_>>());
    }

    #[test]
    fn python_ints() {
        for (text, want) in [
            ("12", Some(12)),
            ("+3", Some(3)),
            ("-3", Some(-3)),
            ("1_000", Some(1000)),
            ("_1", None),
            ("1_", None),
            ("1__0", None),
            ("", None),
            ("-", None),
            ("1.0", None),
            ("0x10", None),
        ] {
            assert_eq!(python_int(text), want, "{text:?}");
        }
    }
}
