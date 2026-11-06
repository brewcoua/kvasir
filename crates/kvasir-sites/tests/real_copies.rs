//! The phase 3 gate: files Python wrote open and answer. They hold private research, so the test
//! works on its own copies and prints counts only.
//!
//! `KVASIR_REAL_DATA=<data dir> cargo nextest run -p kvasir-sites --run-ignored only`

use std::path::{Path, PathBuf};

use kvasir_db::Key;
use kvasir_fetch::PageStore;
use kvasir_sites::favicons::FaviconStore;
use kvasir_sites::sources::SourceStore;
use tempfile::TempDir;

// Long enough that nothing in a real file has expired.
const FOREVER: u64 = 1_000_000;

fn copy_of(name: &str, into: &TempDir) -> PathBuf {
    let data = std::env::var("KVASIR_REAL_DATA").expect("set KVASIR_REAL_DATA");
    let to = into.path().join(name);
    std::fs::copy(Path::new(&data).join(name), &to).unwrap();
    // A live file keeps its recent writes here.
    let wal = Path::new(&data).join(format!("{name}-wal"));
    if wal.exists() {
        std::fs::copy(wal, into.path().join(format!("{name}-wal"))).unwrap();
    }
    to
}

/// The key the copied files were written under.
fn key() -> Key {
    Key::parse(&std::env::var("KVASIR_SECRET_KEY").expect("set KVASIR_SECRET_KEY")).expect("64 hex")
}

fn column(path: &Path, sql: &str) -> Vec<String> {
    let db = key().connect(path).unwrap();
    let mut rows = db.prepare(sql).unwrap();
    rows.query_map([], |row| row.get(0)).unwrap().map(Result::unwrap).collect()
}

#[tokio::test]
#[ignore = "needs a data directory"]
async fn a_real_page_cache_reads_every_page_and_answers_recall() {
    let dir = TempDir::new().unwrap();
    let path = copy_of("pages.db", &dir);
    let store = PageStore::open(&path, &key()).unwrap();

    let rows = store.rows().await.unwrap();
    let mut read = 0;
    for row in &rows {
        read += usize::from(store.get(&row.key).await.unwrap().is_some());
    }
    let stats = store.stats().await.unwrap();
    println!("pages {} read {read} pieces {}", rows.len(), stats.pieces);
    assert_eq!(read, rows.len());
    assert!(read > 0 && stats.pieces > 0);

    let models = column(&path, "SELECT DISTINCT model FROM pieces");
    let texts = column(&path, "SELECT text FROM pieces LIMIT 20");
    let mut found = 0;
    for text in &texts {
        let words: String = text.split_whitespace().take(6).collect::<Vec<_>>().join(" ");
        for model in &models {
            let hits = store.search(&words, model, &[], 5, &Default::default()).await.unwrap();
            assert!(hits.iter().all(|hit| !hit.vector.is_empty()));
            found += hits.len();
        }
    }
    println!("models {} recalled {found}", models.len());
    assert!(found > 0);
}

#[tokio::test]
#[ignore = "needs a data directory"]
async fn a_real_source_cache_reads_every_host_and_its_marks() {
    let dir = TempDir::new().unwrap();
    let path = copy_of("sources.db", &dir);
    let store = SourceStore::open(&path, &key()).unwrap();

    let hosts = column(&path, "SELECT host FROM sources");
    let (mut matches, mut misses) = (0, 0);
    for host in &hosts {
        match store.get(host, FOREVER, FOREVER).await.unwrap().expect("not expired") {
            Some(_) => matches += 1,
            None => misses += 1,
        }
    }
    let marks = store.marks(hosts.clone()).await.unwrap().len();
    println!("hosts {} matches {matches} misses {misses} marks {marks}", hosts.len());
    assert!(!hosts.is_empty());
}

#[tokio::test]
#[ignore = "needs a data directory"]
async fn a_real_favicon_cache_reads_every_host() {
    let dir = TempDir::new().unwrap();
    let path = copy_of("favicons.db", &dir);
    let store = FaviconStore::open(&path, &key()).unwrap();

    let hosts = column(&path, "SELECT host FROM favicons");
    let mut icons = 0;
    for host in &hosts {
        let entry = store.get(host, FOREVER, FOREVER).await.unwrap().expect("not expired");
        icons += usize::from(entry.icon.is_some());
    }
    println!("hosts {} icons {icons}", hosts.len());
    assert!(!hosts.is_empty());
}
