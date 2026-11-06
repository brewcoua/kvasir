//! Runs Python stored read back, and write out as they were. The file holds private research, so
//! the test works on a copy and prints counts only.
//!
//! `KVASIR_SECRET_KEY=<its key> KVASIR_REAL_DATA=<data dir> cargo nextest run -p kvasir-run --run-ignored only`

use std::path::Path;

use kvasir_db::Key;
use kvasir_types::runs::{LmCallContent, RunDetail, RunSummary};
use serde::de::DeserializeOwned;
use serde_json::Value;

/// Fields of older runs that pydantic, ignoring extra fields, already dropped on read.
const OBSOLETE: [&str; 1] = ["round"];

/// Whether `json` reads as `T` and writes back the same value. A field Python left out because
/// an older run predates it may come back filled with its default, so only fields present are
/// compared.
fn same<T: DeserializeOwned + serde::Serialize>(json: &str) -> Result<(), String> {
    let read: T = serde_json::from_str(json).map_err(|e| e.to_string())?;
    let (before, after): (Value, Value) =
        (serde_json::from_str(json).unwrap(), serde_json::to_value(&read).unwrap());
    covers(&after, &before, "$")
}

fn covers(after: &Value, before: &Value, at: &str) -> Result<(), String> {
    match (after, before) {
        (Value::Object(a), Value::Object(b)) => b.iter().try_for_each(|(key, value)| {
            if OBSOLETE.contains(&key.as_str()) {
                return Ok(());
            }
            let got = a.get(key).ok_or(format!("{at}.{key} dropped"))?;
            covers(got, value, &format!("{at}.{key}"))
        }),
        (Value::Array(a), Value::Array(b)) if a.len() == b.len() => a
            .iter()
            .zip(b)
            .enumerate()
            .try_for_each(|(i, (a, b))| covers(a, b, &format!("{at}[{i}]"))),
        (Value::Number(a), Value::Number(b)) if a.as_f64() == b.as_f64() => Ok(()),
        _ if after == before => Ok(()),
        _ => Err(format!("{at}: {before} became {after}")),
    }
}

#[test]
#[ignore = "needs a data directory"]
fn every_stored_run_reads_and_writes_back_unchanged() {
    let data = std::env::var("KVASIR_REAL_DATA").expect("set KVASIR_REAL_DATA");
    let dir = tempfile::TempDir::new().unwrap();
    let copy = dir.path().join("kvasir.db");
    std::fs::copy(Path::new(&data).join("kvasir.db"), &copy).unwrap();
    let wal = Path::new(&data).join("kvasir.db-wal");
    if wal.exists() {
        std::fs::copy(wal, dir.path().join("kvasir.db-wal")).unwrap();
    }
    let key = std::env::var("KVASIR_SECRET_KEY").expect("set KVASIR_SECRET_KEY");
    let db = Key::parse(&key).expect("64 hex").connect(&copy).unwrap();
    let rows: Vec<(String, String)> = db
        .prepare("SELECT summary, detail FROM runs")
        .unwrap()
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))
        .unwrap()
        .map(Result::unwrap)
        .collect();
    let calls: Vec<String> = db
        .prepare("SELECT content FROM calls")
        .unwrap()
        .query_map([], |r| r.get(0))
        .unwrap()
        .map(Result::unwrap)
        .collect();

    let mut failures = Vec::new();
    for (summary, detail) in &rows {
        failures.extend(same::<RunSummary>(summary).err());
        failures.extend(same::<RunDetail>(detail).err());
    }
    for content in &calls {
        failures.extend(same::<LmCallContent>(content).err());
    }
    println!("runs {} calls {} failures {}", rows.len(), calls.len(), failures.len());
    for failure in failures.iter().take(10) {
        println!("{}", &failure[..failure.len().min(300)]);
    }
    assert!(failures.is_empty());
}
