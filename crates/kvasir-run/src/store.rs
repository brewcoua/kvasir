//! Runs kept in SQLite, so they survive a restart. Used when `runs.store` is sqlite.
//!
//! A run is written twice: when it starts, so a restart mid-run leaves a trace of it, and when it
//! ends, with everything observed. The summary is stored beside the detail because a list reads
//! every row, and a detail carries the whole result.

use std::path::Path;

use kvasir_db::schema::KVASIR;
use kvasir_db::{Db, DbError, Key, OptionalExtension, params};
use kvasir_types::runs::{LmCallContent, RunDetail, RunSummary, SearchContent, now};
use serde::de::DeserializeOwned;
use serde_json::Value;

use crate::Run;

const SECONDS_PER_DAY: f64 = 86400.0;
pub const INTERRUPTED: &str = "interrupted by a restart";

#[derive(Clone, Debug)]
pub struct RunStore {
    db: Db,
    ttl_days: u64,
    limit: usize,
}

/// A stored record, or None, with a warning, for one this build cannot read.
fn read<T: DeserializeOwned>(json: &str, what: &str) -> Option<T> {
    serde_json::from_str(json)
        .inspect_err(|e| tracing::warn!("skipping a stored {what} that does not parse: {e}"))
        .ok()
}

fn prune(
    db: &kvasir_db::rusqlite::Connection,
    ttl_days: u64,
    limit: usize,
) -> kvasir_db::rusqlite::Result<()> {
    db.execute(
        "DELETE FROM runs WHERE created_at < ?",
        [now() - ttl_days as f64 * SECONDS_PER_DAY],
    )?;
    db.execute(
        "DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY created_at DESC LIMIT ?)",
        [limit as i64],
    )?;
    db.execute("DELETE FROM calls WHERE run_id NOT IN (SELECT id FROM runs)", [])?;
    db.execute("DELETE FROM searches WHERE run_id NOT IN (SELECT id FROM runs)", [])?;
    Ok(())
}

/// A run saved as it started, ended as failed. Only the start was written, so the start is the
/// last moment the run is known to have been alive.
fn interrupted(record: &str) -> String {
    let Ok(Value::Object(mut data)) = serde_json::from_str::<Value>(record) else {
        return record.to_owned();
    };
    let ended = data.get("started_at").filter(|v| !v.is_null()).or(data.get("created_at")).cloned();
    data.insert("state".into(), "failed".into());
    data.insert("error".into(), INTERRUPTED.into());
    data.insert("ended_at".into(), ended.unwrap_or(Value::Null));
    Value::Object(data).to_string()
}

impl RunStore {
    pub fn open(path: &Path, key: &Key, ttl_days: u64, limit: usize) -> Result<Self, DbError> {
        Ok(Self { db: Db::open(path, key, KVASIR)?, ttl_days, limit })
    }

    /// Losing a record must not fail the run it records, so a write error is only logged.
    pub async fn save(&self, run: &Run) {
        let (id, summary, detail) = (run.id.clone(), run.summary(), run.detail());
        let (contents, searches) = (run.contents(), run.search_contents());
        let (ttl_days, limit) = (self.ttl_days, self.limit);
        let saved = self
            .db
            .call(move |db| {
                let tx = db.transaction()?;
                tx.execute(
                    "INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?)",
                    params![
                        id,
                        summary.created_at,
                        summary.state,
                        serde_json::to_string(&summary).expect("serializes"),
                        serde_json::to_string(&detail).expect("serializes"),
                    ],
                )?;
                for (call, content) in contents {
                    tx.execute(
                        "INSERT OR REPLACE INTO calls VALUES (?, ?, ?)",
                        params![
                            id,
                            call as i64,
                            serde_json::to_string(&content).expect("serializes")
                        ],
                    )?;
                }
                for (search, content) in searches {
                    tx.execute(
                        "INSERT OR REPLACE INTO searches VALUES (?, ?, ?)",
                        params![
                            id,
                            search as i64,
                            serde_json::to_string(&content).expect("serializes")
                        ],
                    )?;
                }
                prune(&tx, ttl_days, limit)?;
                tx.commit()
            })
            .await;
        if let Err(e) = saved {
            tracing::error!("could not save run {}: {e}", run.id);
        }
    }

    /// Newest first.
    pub async fn summaries(&self) -> Result<Vec<RunSummary>, DbError> {
        let limit = self.limit as i64;
        let rows: Vec<String> = self
            .db
            .call(move |db| {
                let mut rows =
                    db.prepare("SELECT summary FROM runs ORDER BY created_at DESC LIMIT ?")?;
                rows.query_map([limit], |r| r.get(0))?.collect()
            })
            .await?;
        Ok(rows.iter().filter_map(|row| read(row, "run")).collect())
    }

    /// The runs whose result holds any of `urls` as a JSON string. Reads each stored result.
    pub async fn citing(&self, urls: &[String]) -> Result<Vec<RunSummary>, DbError> {
        if urls.is_empty() {
            return Ok(Vec::new());
        }
        let needles: Vec<String> =
            urls.iter().map(|url| serde_json::to_string(url).expect("serializes")).collect();
        let found = vec![" instr(json_extract(detail, '$.result'), ?) > 0 "; urls.len()].join("OR");
        let rows: Vec<String> = self
            .db
            .call(move |db| {
                let sql =
                    format!("SELECT summary FROM runs WHERE {found} ORDER BY created_at DESC");
                let mut rows = db.prepare(&sql)?;
                rows.query_map(kvasir_db::rusqlite::params_from_iter(needles), |r| r.get(0))?
                    .collect()
            })
            .await?;
        Ok(rows.iter().filter_map(|row| read(row, "run")).collect())
    }

    pub async fn detail(&self, id: &str) -> Result<Option<RunDetail>, DbError> {
        let id = id.to_owned();
        let row: Option<String> = self
            .db
            .call(move |db| {
                db.query_row("SELECT detail FROM runs WHERE id = ?", [id], |r| r.get(0)).optional()
            })
            .await?;
        Ok(row.and_then(|row| read(&row, "run")))
    }

    pub async fn content(&self, id: &str, call: u64) -> Result<Option<LmCallContent>, DbError> {
        self.one("calls", id, call).await
    }

    pub async fn search_content(
        &self,
        id: &str,
        search: u64,
    ) -> Result<Option<SearchContent>, DbError> {
        self.one("searches", id, search).await
    }

    async fn one<T: DeserializeOwned>(
        &self,
        table: &'static str,
        id: &str,
        item: u64,
    ) -> Result<Option<T>, DbError> {
        let id = id.to_owned();
        let row: Option<String> = self
            .db
            .call(move |db| {
                let sql = format!("SELECT content FROM {table} WHERE run_id = ? AND id = ?");
                db.query_row(&sql, params![id, item as i64], |r| r.get(0)).optional()
            })
            .await?;
        Ok(row.and_then(|row| read(&row, table)))
    }

    /// Drops what is past retention, and ends the runs a restart cut short. Called at startup.
    pub async fn sweep(&self) -> Result<(), DbError> {
        let (ttl_days, limit) = (self.ttl_days, self.limit);
        let ended = self
            .db
            .call(move |db| {
                let tx = db.transaction()?;
                prune(&tx, ttl_days, limit)?;
                let rows: Vec<(String, String, String)> = {
                    let mut rows = tx.prepare(
                        "SELECT id, summary, detail FROM runs WHERE state IN ('queued', 'running')",
                    )?;
                    rows.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?
                        .collect::<Result<_, _>>()?
                };
                for (id, summary, detail) in &rows {
                    tx.execute(
                        "UPDATE runs SET state = 'failed', summary = ?, detail = ? WHERE id = ?",
                        params![interrupted(summary), interrupted(detail), id],
                    )?;
                }
                tx.commit()?;
                Ok(rows.len())
            })
            .await?;
        if ended > 0 {
            tracing::info!("marked {ended} run(s) interrupted by the restart");
        }
        Ok(())
    }
}
