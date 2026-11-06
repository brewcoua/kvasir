//! `sources.db`: the Wikidata answers, which expire, and the marks, which do not.

use std::collections::HashMap;
use std::path::Path;

use kvasir_db::rusqlite::params_from_iter;
use kvasir_db::schema::SOURCES;
use kvasir_db::{Db, DbError, Key, OptionalExtension, params};

use super::Match;
use crate::{SECONDS_PER_DAY, now};

/// Two tables in one file: `sources`, a cache that expires, and `marks`, which never do.
#[derive(Clone, Debug)]
pub struct SourceStore {
    db: Db,
}

impl SourceStore {
    pub fn open(path: &Path, key: &Key) -> Result<Self, DbError> {
        Ok(Self { db: Db::open(path, key, SOURCES)? })
    }

    /// The answer stored for `host`, unless it is older than `ttl_days` (a match) or
    /// `miss_ttl_days` (no match). The inner None is a host Wikidata does not know.
    pub async fn get(
        &self,
        host: &str,
        ttl_days: u64,
        miss_ttl_days: u64,
    ) -> Result<Option<Option<Match>>, DbError> {
        let host = host.to_owned();
        let row = self
            .db
            .call(move |db| {
                db.query_row(
                    "SELECT kind, tags, label, qid, fetched_at FROM sources WHERE host = ?",
                    [host],
                    |r| {
                        let kind: Option<String> = r.get(0)?;
                        let tags: Option<String> = r.get(1)?;
                        let label: Option<String> = r.get(2)?;
                        let qid: Option<String> = r.get(3)?;
                        let fetched_at: f64 = r.get(4)?;
                        let found = kind.map(|kind| Match {
                            kind,
                            tags: tags
                                .unwrap_or_default()
                                .split(',')
                                .filter(|tag| !tag.is_empty())
                                .map(str::to_owned)
                                .collect(),
                            label: label.unwrap_or_default(),
                            qid: qid.unwrap_or_default(),
                        });
                        Ok((found, fetched_at))
                    },
                )
                .optional()
            })
            .await?;
        Ok(row.and_then(|(found, fetched_at)| {
            let days = if found.is_some() { ttl_days } else { miss_ttl_days };
            (now() - fetched_at < days as f64 * SECONDS_PER_DAY).then_some(found)
        }))
    }

    /// Replaces the answer for `host`; no `found` records that Wikidata does not know it.
    pub async fn put(&self, host: &str, found: Option<Match>) -> Result<(), DbError> {
        let host = host.to_owned();
        self.db
            .call(move |db| {
                let (kind, tags, label, qid) = match found {
                    Some(found) => {
                        let tags = found.tags.into_iter().collect::<Vec<_>>().join(",");
                        (Some(found.kind), Some(tags), Some(found.label), Some(found.qid))
                    }
                    None => (None, None, None, None),
                };
                db.execute(
                    "INSERT OR REPLACE INTO sources VALUES (?, ?, ?, ?, ?, ?)",
                    params![host, kind, tags, label, qid, now()],
                )
                .map(|_| ())
            })
            .await
    }

    /// Forgets every cached answer, and keeps the marks.
    pub async fn clear(&self) -> Result<(), DbError> {
        self.db.call(|db| db.execute("DELETE FROM sources", []).map(|_| ())).await
    }

    pub async fn mark(&self, host: &str, value: &str) -> Result<(), DbError> {
        let (host, value) = (host.to_owned(), value.to_owned());
        self.db
            .call(move |db| {
                db.execute(
                    "INSERT OR REPLACE INTO marks VALUES (?, ?, ?)",
                    params![host, value, now()],
                )
                .map(|_| ())
            })
            .await
    }

    pub async fn unmark(&self, host: &str) -> Result<(), DbError> {
        let host = host.to_owned();
        self.db
            .call(move |db| db.execute("DELETE FROM marks WHERE host = ?", [host]).map(|_| ()))
            .await
    }

    /// The marks set on any of `hosts`, by host.
    pub async fn marks(&self, hosts: Vec<String>) -> Result<HashMap<String, String>, DbError> {
        self.db
            .call(move |db| {
                let marks = vec!["?"; hosts.len()].join(",");
                let sql = format!("SELECT host, value FROM marks WHERE host IN ({marks})");
                let mut found = db.prepare(&sql)?;
                let rows =
                    found.query_map(params_from_iter(hosts), |r| Ok((r.get(0)?, r.get(1)?)))?;
                rows.collect()
            })
            .await
    }
}
