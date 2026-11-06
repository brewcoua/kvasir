//! The migrations of each file kvasir keeps, for `Db::open`.
//!
//! Each list's first step accepts the schema Python left at `user_version` 0, so it is written with
//! `IF NOT EXISTS` and matches Python's tables exactly. A later change is a new step at the end.

/// `kvasir.db`: stored settings, and the runs when `runs.store` is sqlite.
pub const KVASIR: &[&str] = &["
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created_at REAL NOT NULL, state TEXT NOT NULL, summary TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS calls (run_id TEXT NOT NULL, id INTEGER NOT NULL, content TEXT NOT NULL, PRIMARY KEY (run_id, id));
CREATE TABLE IF NOT EXISTS searches (run_id TEXT NOT NULL, id INTEGER NOT NULL, content TEXT NOT NULL, PRIMARY KEY (run_id, id));
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- Co-STORM is gone, and so are its sessions' settings. CASE keeps an unreadable summary from
-- failing the step: such a run is skipped when runs are read.
CREATE TEMP TABLE gone AS SELECT id FROM runs
    WHERE CASE WHEN json_valid(summary) THEN json_extract(summary, '$.kind') END = 'costorm';
DELETE FROM calls WHERE run_id IN (SELECT id FROM gone);
DELETE FROM searches WHERE run_id IN (SELECT id FROM gone);
DELETE FROM runs WHERE id IN (SELECT id FROM gone);
DROP TABLE gone;
DELETE FROM settings WHERE key LIKE 'sessions.%';
"];

/// `pages.db`: pages read, their links, and their pieces with vectors and a full-text index.
pub const PAGES: &[&str] = &["
CREATE TABLE IF NOT EXISTS pages (url TEXT PRIMARY KEY, final_url TEXT NOT NULL, fetched_at REAL NOT NULL, etag TEXT, last_modified TEXT, content_hash TEXT NOT NULL, engine TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS links (page TEXT NOT NULL, position INTEGER NOT NULL, target TEXT NOT NULL, anchor TEXT NOT NULL, context TEXT NOT NULL, region TEXT NOT NULL, PRIMARY KEY (page, position));
CREATE TABLE IF NOT EXISTS pieces (page TEXT NOT NULL, position INTEGER NOT NULL, text TEXT NOT NULL, model TEXT NOT NULL, vector BLOB NOT NULL, PRIMARY KEY (page, position));
CREATE VIRTUAL TABLE IF NOT EXISTS pieces_fts USING fts5(text, content='pieces', content_rowid='rowid');
CREATE TRIGGER IF NOT EXISTS pieces_added AFTER INSERT ON pieces BEGIN INSERT INTO pieces_fts(rowid, text) VALUES (new.rowid, new.text); END;
CREATE TRIGGER IF NOT EXISTS pieces_dropped AFTER DELETE ON pieces BEGIN INSERT INTO pieces_fts(pieces_fts, rowid, text) VALUES ('delete', old.rowid, old.text); END;
"];

/// `favicons.db`: site icons, and the sites that have none.
pub const FAVICONS: &[&str] = &["
CREATE TABLE IF NOT EXISTS favicons (host TEXT PRIMARY KEY, body BLOB, content_type TEXT, provider TEXT, fetched_at REAL NOT NULL);
"];

/// `sources.db`: what each site is, and the operator's marks.
pub const SOURCES: &[&str] = &["
CREATE TABLE IF NOT EXISTS marks (host TEXT PRIMARY KEY, value TEXT NOT NULL, marked_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sources (host TEXT PRIMARY KEY, kind TEXT, tags TEXT, label TEXT, qid TEXT, fetched_at REAL NOT NULL);
"];

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Db;
    use rusqlite::Connection;
    use std::path::Path;

    fn golden(name: &str) -> String {
        let path =
            Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/golden/schemas").join(name);
        std::fs::read_to_string(path).unwrap()
    }

    fn schema(conn: &Connection) -> Vec<String> {
        let mut stmt = conn
            .prepare("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name")
            .unwrap();
        stmt.query_map([], |r| r.get(0)).unwrap().map(Result::unwrap).collect()
    }

    fn golden_schema(name: &str) -> Vec<String> {
        let text = golden(name);
        let mut lines: Vec<String> = text
            .lines()
            .filter(|l| !l.starts_with("--"))
            .map(|l| l.trim_end_matches(';').to_owned())
            .collect();
        lines.sort();
        lines
    }

    #[test]
    fn a_new_file_has_the_schema_python_made() {
        let dir = tempfile::tempdir().unwrap();
        for (file, migrations) in
            [("pages.sql", PAGES), ("favicons.sql", FAVICONS), ("sources.sql", SOURCES)]
        {
            let db =
                Db::open(&dir.path().join(file), &crate::Key::for_tests(), migrations).unwrap();
            let mut got = db.call_blocking(|c| Ok(schema(c))).unwrap();
            got.sort();
            assert_eq!(got, golden_schema(file), "{file}");
        }
        let db = Db::open(&dir.path().join("kvasir.db"), &crate::Key::for_tests(), KVASIR).unwrap();
        let mut got = db.call_blocking(|c| Ok(schema(c))).unwrap();
        got.sort();
        let mut want = golden_schema("kvasir.sql");
        want.push("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)".into());
        want.sort();
        assert_eq!(got, want);
    }

    #[test]
    fn a_python_store_loses_co_storm_and_is_backed_up() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("kvasir.db");
        let old = crate::Key::for_tests().connect(&path).unwrap();
        old.execute_batch(&golden("kvasir.sql")).unwrap();
        old.execute_batch(
            "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
             INSERT INTO settings VALUES ('sessions.ttl_hours', '168'), ('storm.max_conv_turn', '2');
             INSERT INTO runs VALUES ('a', 1, 'done', '{\"kind\":\"costorm\"}', '{}'),
                                     ('b', 2, 'done', '{\"kind\":\"graph\"}', '{}'),
                                     ('c', 3, 'done', 'not json', '{}');
             INSERT INTO calls VALUES ('a', 1, 'x'), ('b', 1, 'y');
             INSERT INTO searches VALUES ('a', 1, 'x');",
        )
        .unwrap();
        drop(old);

        let db = Db::open(&path, &crate::Key::for_tests(), KVASIR).unwrap();

        let rows = |sql: &'static str| {
            db.call_blocking(move |c| {
                let mut stmt = c.prepare(sql)?;
                stmt.query_map([], |r| r.get::<_, String>(0))?.collect::<Result<Vec<_>, _>>()
            })
            .unwrap()
        };
        assert_eq!(rows("SELECT id FROM runs ORDER BY id"), ["b", "c"]);
        assert_eq!(rows("SELECT run_id FROM calls"), ["b"]);
        assert!(rows("SELECT run_id FROM searches").is_empty());
        assert_eq!(rows("SELECT key FROM settings"), ["storm.max_conv_turn"]);
        let copy =
            crate::Key::for_tests().connect(dir.path().join("kvasir.db.pre-rust.bak")).unwrap();
        let runs: i64 = copy.query_row("SELECT count(*) FROM runs", [], |r| r.get(0)).unwrap();
        assert_eq!(runs, 3);
    }

    #[test]
    fn a_new_file_is_not_backed_up() {
        let dir = tempfile::tempdir().unwrap();
        Db::open(&dir.path().join("f.db"), &crate::Key::for_tests(), FAVICONS).unwrap();
        assert!(!dir.path().join("f.db.pre-rust.bak").exists());
    }
}

/// Opens a copy of a real `pages.db`. Ignored by default; run with
/// `KVASIR_SECRET_KEY=<its key> KVASIR_REAL_PAGES=<copy of pages.db> cargo test -p kvasir-db -- --ignored`.
#[cfg(test)]
#[test]
#[ignore = "needs a copy of a real pages.db"]
fn a_real_pages_db_opens_and_searches() {
    let path = std::env::var("KVASIR_REAL_PAGES").expect("set KVASIR_REAL_PAGES");
    let key = std::env::var("KVASIR_SECRET_KEY").expect("set KVASIR_SECRET_KEY");
    let key = crate::Key::parse(&key).expect("64 hex characters");
    let db = crate::Db::open(std::path::Path::new(&path), &key, PAGES).unwrap();
    let (pages, pieces, found): (i64, i64, i64) = db
        .call_blocking(|c| {
            Ok((
                c.query_row("SELECT count(*) FROM pages", [], |r| r.get(0))?,
                c.query_row("SELECT count(*) FROM pieces", [], |r| r.get(0))?,
                c.query_row(
                    "SELECT count(*) FROM pieces_fts WHERE pieces_fts MATCH 'the'",
                    [],
                    |r| r.get(0),
                )?,
            ))
        })
        .unwrap();
    println!("{pages} pages, {pieces} pieces, {found} match 'the'");
    assert!(pages > 0 && pieces > 0 && found > 0);
}
