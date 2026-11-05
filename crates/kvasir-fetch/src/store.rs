//! Pages kept in SQLite across runs, so a page read once is not downloaded again while it is fresh.
//!
//! Public page content only: nothing here carries what a user asked.

use std::collections::BTreeSet;
use std::path::Path;
use std::sync::LazyLock;

use kvasir_db::rusqlite::functions::FunctionFlags;
use kvasir_db::rusqlite::{self, params_from_iter};
use kvasir_db::schema::PAGES;
use kvasir_db::{Db, DbError, OptionalExtension, params};
use kvasir_extract::{Link, Region, normalize};
use regex::Regex;
use sha2::{Digest, Sha256};

use crate::{Engine, Page, now};

static WORD: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\w+").expect("a valid pattern"));

/// The id of the page cached under the normalized URL `key`: stateless and safe in a URL.
pub fn page_id(key: &str) -> String {
    hex(&Sha256::digest(key.as_bytes()))[..16].to_owned()
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// A URL's host, without `www.`.
pub fn domain(url: &str) -> String {
    let rest = url.split_once("://").map_or(url, |(_, rest)| rest);
    let host = rest.split(['/', '?', '#']).next().unwrap_or("").to_lowercase();
    host.strip_prefix("www.").map_or_else(|| host.clone(), str::to_owned)
}

/// A cached page without its text or links.
#[derive(Clone, Debug, PartialEq)]
pub struct Row {
    pub key: String,
    pub final_url: String,
    pub title: String,
    pub engine: Engine,
    pub fetched_at: f64,
    pub pieces: i64,
    pub length: i64,
}

/// What the cache holds and what it takes on disk.
#[derive(Clone, Debug, PartialEq)]
pub struct Stats {
    pub pages: i64,
    pub links: i64,
    pub pieces: i64,
    pub text_bytes: i64,
    pub vector_bytes: i64,
    pub file_bytes: u64,
    pub path: String,
    pub oldest: Option<f64>,
    pub newest: Option<f64>,
}

/// A page as it is cached, with its validators.
#[derive(Clone, Debug, PartialEq)]
pub struct Cached {
    pub page: Page,
    pub etag: Option<String>,
    pub last_modified: Option<String>,
}

/// A piece that matched, with its page, which comes without its text or links.
#[derive(Clone, Debug, PartialEq)]
pub struct Found {
    pub page: Page,
    pub text: String,
    pub vector: Vec<f32>,
}

fn engine_of(index: usize, name: String) -> rusqlite::Result<Engine> {
    Engine::parse(&name).ok_or_else(|| {
        let reason = format!("unknown engine {name}");
        rusqlite::Error::FromSqlConversionFailure(index, rusqlite::types::Type::Text, reason.into())
    })
}

fn region_of(index: usize, name: String) -> rusqlite::Result<Region> {
    match name.as_str() {
        "main" => Ok(Region::Main),
        "related" => Ok(Region::Related),
        "nav" => Ok(Region::Nav),
        "footer" => Ok(Region::Footer),
        _ => {
            let reason = format!("unknown region {name}");
            let kind = rusqlite::types::Type::Text;
            Err(rusqlite::Error::FromSqlConversionFailure(index, kind, reason.into()))
        }
    }
}

fn region_name(region: Region) -> &'static str {
    match region {
        Region::Main => "main",
        Region::Related => "related",
        Region::Nav => "nav",
        Region::Footer => "footer",
    }
}

/// Three tables and a text index in one file.
#[derive(Clone, Debug)]
pub struct PageStore {
    db: Db,
}

impl PageStore {
    /// Opens `path`, moving any page stored under a URL `normalize` now spells otherwise.
    pub fn open(path: &Path) -> Result<Self, DbError> {
        let db = Db::open(path, PAGES)?;
        db.call_blocking(rekey)?;
        Ok(Self { db })
    }

    pub fn path(&self) -> &Path {
        self.db.path()
    }

    /// The page cached for `url`; its `url` is as given.
    pub async fn get(&self, url: &str) -> Result<Option<Cached>, DbError> {
        let Ok(key) = normalize(url) else {
            return Ok(None);
        };
        let url = url.to_owned();
        self.db
            .call(move |db| {
                let row = db
                    .query_row(
                        "SELECT final_url, fetched_at, etag, last_modified, engine, title, text \
                         FROM pages WHERE url = ?",
                        [&key],
                        |r| {
                            Ok(Cached {
                                page: Page {
                                    url,
                                    final_url: r.get(0)?,
                                    fetched_at: r.get(1)?,
                                    engine: engine_of(4, r.get(4)?)?,
                                    title: r.get(5)?,
                                    text: r.get(6)?,
                                    links: Vec::new(),
                                },
                                etag: r.get(2)?,
                                last_modified: r.get(3)?,
                            })
                        },
                    )
                    .optional()?;
                let Some(mut cached) = row else {
                    return Ok(None);
                };
                let mut links = db.prepare(
                    "SELECT target, anchor, context, region FROM links WHERE page = ? \
                     ORDER BY position",
                )?;
                cached.page.links = links
                    .query_map([&key], |r| {
                        Ok(Link {
                            url: r.get(0)?,
                            anchor: r.get(1)?,
                            context: r.get(2)?,
                            region: region_of(3, r.get(3)?)?,
                        })
                    })?
                    .collect::<Result<_, _>>()?;
                Ok(Some(cached))
            })
            .await
    }

    /// Every page, newest first, without its text. Scans the table, which is small.
    pub async fn rows(&self) -> Result<Vec<Row>, DbError> {
        self.db
            .call(|db| {
                let mut rows = db.prepare(
                    "SELECT url, final_url, title, engine, fetched_at, \
                     (SELECT COUNT(*) FROM pieces WHERE pieces.page = pages.url), length(text) \
                     FROM pages ORDER BY fetched_at DESC",
                )?;
                let found = rows.query_map([], |r| {
                    Ok(Row {
                        key: r.get(0)?,
                        final_url: r.get(1)?,
                        title: r.get(2)?,
                        engine: engine_of(3, r.get(3)?)?,
                        fetched_at: r.get(4)?,
                        pieces: r.get(5)?,
                        length: r.get(6)?,
                    })
                })?;
                found.collect()
            })
            .await
    }

    /// Row counts, bytes of text and vectors, and the file with its WAL and shared-memory files.
    pub async fn stats(&self) -> Result<Stats, DbError> {
        let path = self.path().to_owned();
        self.db
            .call(move |db| {
                let (pages, text_bytes, oldest, newest) = db.query_row(
                    "SELECT COUNT(*), COALESCE(SUM(length(CAST(text AS BLOB))), 0), \
                     MIN(fetched_at), MAX(fetched_at) FROM pages",
                    [],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
                )?;
                let links = db.query_row("SELECT COUNT(*) FROM links", [], |r| r.get(0))?;
                let (pieces, vector_bytes) = db.query_row(
                    "SELECT COUNT(*), COALESCE(SUM(length(vector)), 0) FROM pieces",
                    [],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )?;
                let text = path.to_string_lossy();
                let file_bytes = [text.to_string(), format!("{text}-wal"), format!("{text}-shm")]
                    .iter()
                    .filter_map(|file| std::fs::metadata(file).ok())
                    .map(|meta| meta.len())
                    .sum();
                Ok(Stats {
                    pages,
                    links,
                    pieces,
                    text_bytes,
                    vector_bytes,
                    file_bytes,
                    path: text.into_owned(),
                    oldest,
                    newest,
                })
            })
            .await
    }

    /// The page and target URL of every stored link.
    pub async fn link_pairs(&self) -> Result<Vec<(String, String)>, DbError> {
        self.db
            .call(|db| {
                let mut links = db.prepare("SELECT page, target FROM links")?;
                let found = links.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?;
                found.collect()
            })
            .await
    }

    /// Drops every page, link and piece, then returns the space to the disk.
    pub async fn flush(&self) -> Result<(), DbError> {
        self.db
            .call(|db| {
                db.execute_batch(
                    "BEGIN; DELETE FROM pages; DELETE FROM links; DELETE FROM pieces; COMMIT;",
                )?;
                // It writes through the WAL, so the checkpoint that empties the WAL comes after.
                db.execute_batch("VACUUM; PRAGMA wal_checkpoint(TRUNCATE);")
            })
            .await
    }

    /// Drops the pages cached under the normalized URLs `keys`, with their links and pieces.
    pub async fn delete(&self, keys: Vec<String>) -> Result<(), DbError> {
        self.db
            .call(move |db| {
                let tx = db.transaction()?;
                for key in &keys {
                    delete_page(&tx, key)?;
                }
                tx.commit()
            })
            .await
    }

    /// Replaces the page cached for its URL, and its links, in one transaction. Its pieces belonged
    /// to the old text, so they go.
    pub async fn put(
        &self,
        page: &Page,
        etag: Option<&str>,
        last_modified: Option<&str>,
    ) -> Result<(), DbError> {
        let Ok(key) = normalize(&page.url) else {
            return Ok(());
        };
        let page = page.clone();
        let (etag, last_modified) = (etag.map(str::to_owned), last_modified.map(str::to_owned));
        self.db
            .call(move |db| {
                let digest = hex(&Sha256::digest(page.text.as_bytes()));
                let tx = db.transaction()?;
                tx.execute(
                    "INSERT OR REPLACE INTO pages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    params![
                        key,
                        page.final_url,
                        page.fetched_at,
                        etag,
                        last_modified,
                        digest,
                        page.engine.name(),
                        page.title,
                        page.text,
                    ],
                )?;
                tx.execute("DELETE FROM links WHERE page = ?", [&key])?;
                tx.execute("DELETE FROM pieces WHERE page = ?", [&key])?;
                {
                    let mut insert = tx.prepare("INSERT INTO links VALUES (?, ?, ?, ?, ?, ?)")?;
                    for (position, link) in page.links.iter().enumerate() {
                        insert.execute(params![
                            key,
                            position as i64,
                            link.url,
                            link.anchor,
                            link.context,
                            region_name(link.region),
                        ])?;
                    }
                }
                tx.commit()
            })
            .await
    }

    /// Marks the page as read now, after its site said it had not changed. Returns that time.
    pub async fn touch(&self, url: &str) -> Result<f64, DbError> {
        let at = now();
        let Ok(key) = normalize(url) else {
            return Ok(at);
        };
        self.db
            .call(move |db| {
                db.execute("UPDATE pages SET fetched_at = ? WHERE url = ?", params![at, key])
            })
            .await?;
        Ok(at)
    }

    /// Replaces the pieces kept for `url` with `pieces`, each a text and its vector from `model`.
    pub async fn put_pieces(
        &self,
        url: &str,
        pieces: Vec<(String, Vec<f32>)>,
        model: &str,
    ) -> Result<(), DbError> {
        let Ok(key) = normalize(url) else {
            return Ok(());
        };
        let model = model.to_owned();
        self.db
            .call(move |db| {
                let tx = db.transaction()?;
                tx.execute("DELETE FROM pieces WHERE page = ?", [&key])?;
                {
                    let mut insert = tx.prepare("INSERT INTO pieces VALUES (?, ?, ?, ?, ?)")?;
                    for (position, (text, vector)) in pieces.iter().enumerate() {
                        let bytes: Vec<u8> =
                            vector.iter().flat_map(|value| value.to_le_bytes()).collect();
                        insert.execute(params![key, position as i64, text, model, bytes])?;
                    }
                }
                tx.commit()
            })
            .await
    }

    /// The pieces of `model` that best match the words of `words` (BM25), at most `limit`, leaving
    /// out the pages in `exclude_urls` and those of the sites in `exclude_hosts`.
    pub async fn search(
        &self,
        words: &str,
        model: &str,
        exclude_urls: &[String],
        limit: usize,
        exclude_hosts: &BTreeSet<String>,
    ) -> Result<Vec<Found>, DbError> {
        let terms: Vec<String> =
            WORD.find_iter(words).map(|word| format!("\"{}\"", word.as_str())).collect();
        if terms.is_empty() {
            return Ok(Vec::new());
        }
        let excluded: Vec<String> =
            exclude_urls.iter().filter_map(|url| normalize(url).ok()).collect();
        let hosts: Vec<String> = exclude_hosts.iter().cloned().collect();
        let model = model.to_owned();
        self.db
            .call(move |db| {
                db.create_scalar_function(
                    "host",
                    1,
                    FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC,
                    |context| Ok(domain(&context.get::<String>(0)?)),
                )?;
                let marks = |count: usize| vec!["?"; count].join(",");
                let sql = format!(
                    "SELECT pages.url, pages.final_url, pages.title, pages.engine, \
                     pages.fetched_at, pieces.text, pieces.vector FROM pieces_fts \
                     JOIN pieces ON pieces.rowid = pieces_fts.rowid \
                     JOIN pages ON pages.url = pieces.page \
                     WHERE pieces_fts MATCH ? AND pieces.model = ? \
                     AND pieces.page NOT IN ({}) AND host(pages.url) NOT IN ({}) \
                     ORDER BY bm25(pieces_fts) LIMIT {limit}",
                    marks(excluded.len()),
                    marks(hosts.len()),
                );
                let mut values = vec![terms.join(" OR "), model];
                values.extend(excluded);
                values.extend(hosts);
                let mut found = db.prepare(&sql)?;
                let rows = found.query_map(params_from_iter(values), |r| {
                    let bytes: Vec<u8> = r.get(6)?;
                    Ok(Found {
                        page: Page {
                            url: r.get(0)?,
                            final_url: r.get(1)?,
                            title: r.get(2)?,
                            text: String::new(),
                            engine: engine_of(3, r.get(3)?)?,
                            links: Vec::new(),
                            fetched_at: r.get(4)?,
                        },
                        text: r.get(5)?,
                        vector: bytes
                            .as_chunks::<4>()
                            .0
                            .iter()
                            .map(|c| f32::from_le_bytes(*c))
                            .collect(),
                    })
                })?;
                rows.collect()
            })
            .await
    }
}

fn delete_page(db: &rusqlite::Connection, key: &str) -> rusqlite::Result<()> {
    db.execute("DELETE FROM pages WHERE url = ?", [key])?;
    db.execute("DELETE FROM links WHERE page = ?", [key])?;
    db.execute("DELETE FROM pieces WHERE page = ?", [key])?;
    Ok(())
}

/// Moves pages stored under another spelling to the one `normalize` writes, keeping the newest of
/// any that collide. Nothing to do on a store written by this code, and quick to find that out.
fn rekey(db: &mut rusqlite::Connection) -> rusqlite::Result<()> {
    let stored: Vec<String> = {
        let mut pages = db.prepare("SELECT url FROM pages")?;
        let rows = pages.query_map([], |r| r.get(0))?;
        rows.collect::<Result<_, _>>()?
    };
    let moves: Vec<(String, String)> = stored
        .into_iter()
        .filter_map(|url| normalize(&url).ok().filter(|key| *key != url).map(|key| (url, key)))
        .collect();
    if moves.is_empty() {
        return Ok(());
    }
    let tx = db.transaction()?;
    for (old, new) in &moves {
        let newest = |key: &str| {
            tx.query_row("SELECT fetched_at FROM pages WHERE url = ?", [key], |r| {
                r.get::<_, f64>(0)
            })
            .optional()
        };
        if let Some(there) = newest(new)? {
            if there >= newest(old)?.unwrap_or(f64::MIN) {
                delete_page(&tx, old)?;
                continue;
            }
            delete_page(&tx, new)?;
        }
        tx.execute("UPDATE pages SET url = ? WHERE url = ?", [new, old])?;
        tx.execute("UPDATE links SET page = ? WHERE page = ?", [new, old])?;
        tx.execute("UPDATE pieces SET page = ? WHERE page = ?", [new, old])?;
    }
    tx.commit()?;
    tracing::info!(pages = moves.len(), "moved stored pages to their normalized URLs");
    Ok(())
}
