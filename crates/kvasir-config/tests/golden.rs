//! Loads every case in `tests/golden/settings.json`, which Python's `Settings.load` produced, and
//! expects the same settings, warnings and errors.

use std::path::Path;

use kvasir_config::Settings;
use serde_json::{Map, Value, json};

fn dump(s: &Settings) -> Value {
    let providers: Map<String, Value> = s
        .providers
        .iter()
        .map(|(name, p)| (name.clone(), json!({"api_base": p.api_base, "api_key": p.api_key})))
        .collect();
    json!({
        "data_dir": s.data_dir,
        "token": s.token,
        "host": s.host,
        "port": s.port,
        "max_threads": s.max_threads,
        "providers": providers,
        "log": s.log,
        "model": s.model,
        // The fields Python had, as they are called now.
        "search": {"url": s.search.searxng.url, "top_k": s.search.top_k},
        "runs": s.runs,
        "research": s.research,
        "storm": s.storm,
        "fetch": s.fetch,
        "favicons": {
            "providers": s.favicons.providers,
            "custom_url": s.favicons.custom_url,
            "ttl_days": s.favicons.ttl_days,
            "miss_ttl_days": s.favicons.miss_ttl_days,
        },
        "sources": s.sources,
        // Python required its one search URL.
        "missing": s.missing().iter().map(|key| {
            if *key == kvasir_config::WEB_PROVIDER { "search.searxng.url" } else { key }
        }).collect::<Vec<_>>(),
    })
}

#[test]
fn the_secret_key_is_required_and_checked() {
    let token = || ("KVASIR_TOKEN".to_owned(), "t".to_owned());
    let load = |env: Vec<(String, String)>| Settings::from_env(env, &mut vec![]);

    assert_eq!(
        load(vec![token()]).unwrap_err().to_string(),
        "missing required environment variables: KVASIR_SECRET_KEY"
    );
    assert_eq!(
        load(vec![token(), ("KVASIR_SECRET_KEY".into(), "short".into())]).unwrap_err().to_string(),
        "KVASIR_SECRET_KEY must be 64 hex characters, as from openssl rand -hex 32"
    );
    let good = load(vec![token(), ("KVASIR_SECRET_KEY".into(), "ab".repeat(32))]).unwrap();
    assert!(!format!("{good:?}").contains(&"ab".repeat(32)));
}

#[test]
fn settings_load_as_python_loaded_them() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/settings.json");
    // Python had `search.url`.
    let text = std::fs::read_to_string(path).unwrap().replace("search.url", "search.searxng.url");
    let cases: Vec<Value> = serde_json::from_str(&text).unwrap();
    let mut failed = Vec::new();
    for case in &cases {
        let mut env: Vec<(String, String)> = serde_json::from_value(case["env"].clone()).unwrap();
        // Python had no such variable, and each case's outcome is about the others.
        env.insert(0, ("KVASIR_SECRET_KEY".into(), "ab".repeat(32)));
        let mut stored = case["stored"].as_object().unwrap().clone();
        // Python had no switch: its URL was its search.
        if stored.contains_key("search.searxng.url") {
            stored.insert("search.searxng.enabled".into(), true.into());
        }
        let strict = case["strict"].as_bool().unwrap();
        let mut warnings = Vec::new();
        let got = Settings::from_env(env, &mut warnings)
            .and_then(|s| s.with_stored(&stored, strict, &mut warnings));
        let mut want = case.clone();
        for key in ["env", "stored", "strict"] {
            want.as_object_mut().unwrap().remove(key);
        }
        let mut have = json!({"name": case["name"], "warnings": warnings});
        match got {
            Ok(settings) => have["settings"] = dump(&settings),
            Err(err) => have["error"] = err.to_string().into(),
        }
        if have != want {
            failed.push(format!("want {want}\nhave {have}"));
        }
    }
    assert!(
        failed.is_empty(),
        "{} of {} cases differ:\n{}",
        failed.len(),
        cases.len(),
        failed.join("\n\n")
    );
}
