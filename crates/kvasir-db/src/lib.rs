//! Opening kvasir's SQLite files, migrating them by `user_version`, and running blocking database
//! work off the async runtime.
//!
//! Each file has one connection behind a mutex. The service's writes are small and rare, so
//! serialising them costs nothing, and WAL lets a reader in another process see a consistent file.

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use rusqlite::Connection;
pub use rusqlite::{self, OptionalExtension, params};

/// How long a statement waits for another process's lock before failing.
pub const BUSY_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Debug, thiserror::Error)]
pub enum DbError {
    #[error("cannot create {path}: {source}")]
    Create { path: PathBuf, source: std::io::Error },
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
    /// Opens `path`, creating it and its directory, in WAL mode, then applies each of `migrations`
    /// past the file's `user_version`, one transaction each.
    ///
    /// The first migration of each file must accept the schema Python left at `user_version` 0,
    /// so it is written with `IF NOT EXISTS`.
    pub fn open(path: &Path, migrations: &[&str]) -> Result<Self, DbError> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)
                .map_err(|source| DbError::Create { path: dir.to_owned(), source })?;
        }
        let mut conn = Connection::open(path)?;
        conn.busy_timeout(BUSY_TIMEOUT)?;
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
    for (version, sql) in migrations.iter().enumerate().skip(found).map(|(i, sql)| (i + 1, sql)) {
        let fail = |source| DbError::Migrate { path: path.to_owned(), version, source };
        let tx = conn.transaction().map_err(fail)?;
        tx.execute_batch(sql).map_err(fail)?;
        tx.pragma_update(None, "user_version", version as i64).map_err(fail)?;
        tx.commit().map_err(fail)?;
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
        let db = Db::open(&dir.path().join("a/b.db"), MIGRATIONS).unwrap();

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
        let old = Connection::open(&path).unwrap();
        old.execute_batch(
            "CREATE TABLE t (k TEXT PRIMARY KEY, v TEXT); INSERT INTO t VALUES ('a', 'b');",
        )
        .unwrap();
        drop(old);

        let db = Db::open(&path, MIGRATIONS).unwrap();

        let v: String =
            db.call_blocking(|c| c.query_row("SELECT v FROM t", [], |r| r.get(0))).unwrap();
        assert_eq!(v, "b");
    }

    #[test]
    fn a_failed_migration_leaves_the_version_where_it_was() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("d.db");
        Db::open(&path, MIGRATIONS).unwrap();

        let err =
            Db::open(&path, &[MIGRATIONS[0], "ALTER TABLE t ADD w TEXT; nonsense"]).unwrap_err();

        assert!(matches!(err, DbError::Migrate { version: 2, .. }));
        let db = Db::open(&path, MIGRATIONS).unwrap();
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
        Db::open(&path, &[MIGRATIONS[0], "SELECT 1"]).unwrap();

        assert!(matches!(
            Db::open(&path, MIGRATIONS),
            Err(DbError::TooNew { found: 2, known: 1, .. })
        ));
    }

    #[tokio::test]
    async fn work_runs_off_the_runtime() {
        let dir = tempfile::tempdir().unwrap();
        let db = Db::open(&dir.path().join("c.db"), MIGRATIONS).unwrap();

        db.call(|c| c.execute("INSERT INTO t VALUES ('x', 'y')", []).map(|_| ())).await.unwrap();
        let n: i64 =
            db.call(|c| c.query_row("SELECT count(*) FROM t", [], |r| r.get(0))).await.unwrap();

        assert_eq!(n, 1);
    }
}
