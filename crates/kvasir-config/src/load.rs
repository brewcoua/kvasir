//! Building `Settings`: from the environment, then from the stored rows, each value checked.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use kvasir_db::Key;
use kvasir_types::sites::{KINDS, LIST_TAGS, is_list_label, normalize_host};
use serde::Serialize;
use serde::de::DeserializeOwned;
use serde_json::{Map, Value};

use crate::fields::{self, FIELDS, Kind, MODEL_KEYS, field_for};
use crate::repr::text as quote;
use crate::{
    ConfigError, FaviconProvider, FaviconSettings, FetchSettings, LogFormat, LogSettings,
    ModelSettings, NO_API_KEY, Provider, ResearchSettings, RunSettings, SearchSettings, Settings,
    SourceSettings, StormSettings,
};

const PROVIDER_PREFIX: &str = "KVASIR_PROVIDER_";
const ENV_NAMES: [&str; 8] = [
    "KVASIR_DATA_DIR",
    "KVASIR_TOKEN",
    "KVASIR_SECRET_KEY",
    "KVASIR_HOST",
    "KVASIR_PORT",
    "KVASIR_MAX_THREADS",
    "KVASIR_LOG_LEVEL",
    "KVASIR_LOG_FORMAT",
];

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
        if get("KVASIR_SECRET_KEY").is_none() {
            missing.push("KVASIR_SECRET_KEY".into());
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
        let secret_key =
            Key::parse(get("KVASIR_SECRET_KEY").expect("checked above")).ok_or_else(|| {
                ConfigError::new(
                    "KVASIR_SECRET_KEY must be 64 hex characters, as from openssl rand -hex 32",
                )
            })?;
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
            secret_key,
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
            Err(_) if field_for(&key).is_some_and(|f| f.kind == Kind::Secret) => {
                problem(format!("{key} is not JSON"), strict, warnings)?;
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
    for (name, value) in values {
        let mut at = &mut section;
        for part in name.split('.') {
            at = &mut at[part];
        }
        *at = value;
    }
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
