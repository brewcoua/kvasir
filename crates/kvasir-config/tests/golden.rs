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
        "search": s.search,
        "runs": s.runs,
        "research": s.research,
        "storm": s.storm,
        "fetch": s.fetch,
        "favicons": s.favicons,
        "sources": s.sources,
        "missing": s.missing(),
    })
}

#[test]
fn settings_load_as_python_loaded_them() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/settings.json");
    let cases: Vec<Value> = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let mut failed = Vec::new();
    for case in &cases {
        let env: Vec<(String, String)> = serde_json::from_value(case["env"].clone()).unwrap();
        let stored = case["stored"].as_object().unwrap();
        let strict = case["strict"].as_bool().unwrap();
        let mut warnings = Vec::new();
        let got = Settings::from_env(env, &mut warnings)
            .and_then(|s| s.with_stored(stored, strict, &mut warnings));
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
