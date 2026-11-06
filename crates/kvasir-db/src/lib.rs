#![doc = include_str!("../README.md")]

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use rusqlite::Connection;
pub use rusqlite::{self, OptionalExtension, params};

pub mod schema;

/// How long a statement waits for another process's lock before failing.
pub const BUSY_TIMEOUT: Duration = Duration::from_secs(30);

/// What every database file is encrypted under: 32 random bytes, as 64 hex characters.
#[derive(Clone, PartialEq, Eq)]
pub struct Key(String);

impl std::fmt::Debug for Key {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Key(..)")
    }
}

impl Key {
    /// `None` unless `hex` is 64 hex characters.
    pub fn parse(hex: &str) -> Option<Self> {
        (hex.len() == 64 && hex.bytes().all(|b| b.is_ascii_hexdigit()))
            .then(|| Self(hex.to_owned()))
    }

    /// A fixed key, for tests.
    pub fn for_tests() -> Self {
        Self("ab".repeat(32))
    }

    /// Opens `path` under this key. Fails with `NotADatabase` if the key does not open it.
    pub fn connect(&self, path: impl AsRef<Path>) -> Result<Connection, rusqlite::Error> {
        let conn = Connection::open(path)?;
        // A raw key, since it is random already: a passphrase's PBKDF2 would run at every open.
        conn.execute_batch(&format!("PRAGMA key = \"x'{}'\"", self.0))?;
        // SQLCipher does not encrypt temporary files, so there are none.
        conn.pragma_update(None, "temp_store", "memory")?;
        conn.busy_timeout(BUSY_TIMEOUT)?;
        // The key is only checked when a page is read.
        conn.query_row("SELECT count(*) FROM sqlite_master", [], |_| Ok(()))?;
        Ok(conn)
    }
}

#[derive(Debug, thiserror::Error)]
pub enum DbError {
    #[error("cannot create {path}: {source}")]
    Create { path: PathBuf, source: std::io::Error },
    #[error("{path}: KVASIR_SECRET_KEY does not open it; it was encrypted with another key")]
    WrongKey { path: PathBuf },
    #[error(transparent)]
    Sqlite(#[from] rusqlite::Error),
    #[error("migrating {path} to version {version}: {source}")]
    Migrate { path: PathBuf, version: usize, source: rusqlite::Error },
    #[error("{path} is at version {found}, newer than this build's {known}")]
    TooNew { path: PathBuf, found: usize, known: usize },
    #[error("database task panicked")]
    Panicked,
}

/// One SQLite file. Cheap to clone: clones share the connection.
#[derive(Clone)]
pub struct Db {
    path: Arc<PathBuf>,
    conn: Arc<Mutex<Connection>>,
}

impl std::fmt::Debug for Db {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Db").field("path", &self.path).finish_non_exhaustive()
    }
}

impl Db {
    /// Opens `path` under `key`, creating it and its directory, in WAL mode, then applies each of
    /// `migrations` past the file's `user_version`, one transaction each.
    ///
    /// The first migration of each file must accept the schema Python left at `user_version` 0,
    /// so it is written with `IF NOT EXISTS`.
    pub fn open(path: &Path, key: &Key, migrations: &[&str]) -> Result<Self, DbError> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)
                .map_err(|source| DbError::Create { path: dir.to_owned(), source })?;
        }
        let mut conn = key.connect(path).map_err(|err| match err.sqlite_error_code() {
            Some(rusqlite::ErrorCode::NotADatabase) => DbError::WrongKey { path: path.to_owned() },
            _ => err.into(),
        })?;
        conn.pragma_update(None, "journal_mode", "wal")?;
        migrate(&mut conn, path, migrations)?;
        Ok(Self { path: Arc::new(path.to_owned()), conn: Arc::new(Mutex::new(conn)) })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Runs `work` on the blocking pool with the connection.
    pub async fn call<T, F>(&self, work: F) -> Result<T, DbError>
    where
        T: Send + 'static,
        F: FnOnce(&mut Connection) -> Result<T, rusqlite::Error> + Send + 'static,
    {
        let conn = Arc::clone(&self.conn);
        tokio::task::spawn_blocking(move || {
            // A panic in another call poisons the lock but cannot leave a transaction open: an
            // unfinished `Transaction` rolls back when dropped.
            let mut conn = conn.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
            work(&mut conn)
        })
        .await
        .map_err(|_| DbError::Panicked)?
        .map_err(DbError::from)
    }

    /// Runs `work` with the connection on this thread, for startup and tests.
    pub fn call_blocking<T>(
        &self,
        work: impl FnOnce(&mut Connection) -> Result<T, rusqlite::Error>,
    ) -> Result<T, DbError> {
        let mut conn = self.conn.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        Ok(work(&mut conn)?)
    }
}

fn migrate(conn: &mut Connection, path: &Path, migrations: &[&str]) -> Result<(), DbError> {
    let found: i64 = conn.pragma_query_value(None, "user_version", |r| r.get(0))?;
    // A negative version is not one this or Python's code ever writes.
    let found = usize::try_from(found).unwrap_or(usize::MAX);
    if found > migrations.len() {
        return Err(DbError::TooNew { path: path.to_owned(), found, known: migrations.len() });
    }
    if found == 0 && !migrations.is_empty() {
        backup(conn, path)?;
    }
    for (version, sql) in migrations.iter().enumerate().skip(found).map(|(i, sql)| (i + 1, sql)) {
        let fail = |source| DbError::Migrate { path: path.to_owned(), version, source };
        let tx = conn.transaction().map_err(fail)?;
        tx.execute_batch(sql).map_err(fail)?;
        tx.pragma_update(None, "user_version", version as i64).map_err(fail)?;
        tx.commit().map_err(fail)?;
    }
    Ok(())
}

/// Copies a file Python left, at version 0 with tables in it, to `<file>.pre-rust.bak`, since the
/// migrations may leave it unreadable to Python. An existing copy is kept: it is the older one.
fn backup(conn: &Connection, path: &Path) -> Result<(), DbError> {
    let tables: i64 = conn.query_row("SELECT count(*) FROM sqlite_master", [], |r| r.get(0))?;
    let mut name = path.as_os_str().to_owned();
    name.push(".pre-rust.bak");
    let copy = PathBuf::from(name);
    if tables > 0 && !copy.exists() {
        conn.execute("VACUUM INTO ?1", [copy.to_string_lossy()])?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    const MIGRATIONS: &[&str] = &["CREATE TABLE IF NOT EXISTS t (k TEXT PRIMARY KEY, v TEXT)"];

    #[test]
    fn a_new_file_is_created_in_wal_mode_and_migrated() {
        let dir = tempfile::tempdir().unwrap();
        let db = Db::open(&dir.path().join("a/b.db"), &Key::for_tests(), MIGRATIONS).unwrap();

        let (mode, version): (String, i64) = db
            .call_blocking(|c| {
                Ok((
                    c.pragma_query_value(None, "journal_mode", |r| r.get(0))?,
                    c.pragma_query_value(None, "user_version", |r| r.get(0))?,
                ))
            })
            .unwrap();

        assert_eq!((mode.as_str(), version), ("wal", 1));
    }

    #[test]
    fn a_file_python_made_is_taken_over_without_losing_rows() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("old.db");
        let old = Key::for_tests().connect(&path).unwrap();
        old.execute_batch(
            "CREATE TABLE t (k TEXT PRIMARY KEY, v TEXT); INSERT INTO t VALUES ('a', 'b');",
        )
        .unwrap();
        drop(old);

        let db = Db::open(&path, &Key::for_tests(), MIGRATIONS).unwrap();

        let v: String =
            db.call_blocking(|c| c.query_row("SELECT v FROM t", [], |r| r.get(0))).unwrap();
        assert_eq!(v, "b");
    }

    #[test]
    fn a_failed_migration_leaves_the_version_where_it_was() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("d.db");
        Db::open(&path, &Key::for_tests(), MIGRATIONS).unwrap();

        let err = Db::open(
            &path,
            &Key::for_tests(),
            &[MIGRATIONS[0], "ALTER TABLE t ADD w TEXT; nonsense"],
        )
        .unwrap_err();

        assert!(matches!(err, DbError::Migrate { version: 2, .. }));
        let db = Db::open(&path, &Key::for_tests(), MIGRATIONS).unwrap();
        let columns: i64 = db
            .call_blocking(|c| {
                c.query_row("SELECT count(*) FROM pragma_table_info('t')", [], |r| r.get(0))
            })
            .unwrap();
        assert_eq!(columns, 2);
    }

    #[test]
    fn a_file_from_a_newer_build_is_refused() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("e.db");
        Db::open(&path, &Key::for_tests(), &[MIGRATIONS[0], "SELECT 1"]).unwrap();

        assert!(matches!(
            Db::open(&path, &Key::for_tests(), MIGRATIONS),
            Err(DbError::TooNew { found: 2, known: 1, .. })
        ));
    }

    #[test]
    fn a_key_is_64_hex_characters() {
        assert!(Key::parse(&"aB".repeat(32)).is_some());
        for bad in ["", "ab", &"ab".repeat(33), &"zz".repeat(32)] {
            assert!(Key::parse(bad).is_none(), "{bad}");
        }
    }

    #[test]
    fn a_key_does_not_show_in_debug() {
        assert_eq!(format!("{:?}", Key::for_tests()), "Key(..)");
    }

    #[test]
    fn a_file_is_refused_under_another_key() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("k.db");
        Db::open(&path, &Key::for_tests(), MIGRATIONS).unwrap();

        let other = Key::parse(&"cd".repeat(32)).unwrap();

        assert!(matches!(Db::open(&path, &other, MIGRATIONS), Err(DbError::WrongKey { .. })));
    }

    #[test]
    fn nothing_written_shows_in_the_files() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("p.db");
        let db = Db::open(&path, &Key::for_tests(), MIGRATIONS).unwrap();
        db.call_blocking(|c| c.execute("INSERT INTO t VALUES ('k', 'needle-in-the-file')", []))
            .unwrap();

        let needle: &[u8] = b"needle-in-the-file";
        for file in std::fs::read_dir(dir.path()).unwrap() {
            let bytes = std::fs::read(file.unwrap().path()).unwrap();
            assert!(!bytes.windows(needle.len()).any(|w| w == needle));
        }
        drop(db);
        let bytes = std::fs::read(&path).unwrap();
        assert!(!bytes.windows(needle.len()).any(|w| w == needle));
        assert!(!bytes.starts_with(b"SQLite format 3"));
    }

    #[tokio::test]
    async fn work_runs_off_the_runtime() {
        let dir = tempfile::tempdir().unwrap();
        let db = Db::open(&dir.path().join("c.db"), &Key::for_tests(), MIGRATIONS).unwrap();

        db.call(|c| c.execute("INSERT INTO t VALUES ('x', 'y')", []).map(|_| ())).await.unwrap();
        let n: i64 =
            db.call(|c| c.query_row("SELECT count(*) FROM t", [], |r| r.get(0))).await.unwrap();

        assert_eq!(n, 1);
    }
}
