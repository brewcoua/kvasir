#![doc = include_str!("../README.md")]

mod fields;
mod load;
mod repr;
mod sections;
pub mod store;

use std::collections::BTreeMap;
use std::path::PathBuf;

use serde_json::Value;

pub use fields::{FIELDS, Field, Kind, MODEL_KEYS, field_for};
pub use load::decode;
use repr::text as quote;
pub use sections::{
    FaviconProvider, FaviconSettings, FetchSettings, LogFormat, LogSettings, ModelSettings, Render,
    ResearchSettings, RunSettings, RunStore, SearchSettings, SourceSettings, StormSettings,
};

/// Sent when a provider has no key: the OpenAI client refuses an empty one, and Ollama ignores it.
pub const NO_API_KEY: &str = "none";

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
}
