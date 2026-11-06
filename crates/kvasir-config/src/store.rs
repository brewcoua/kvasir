//! The `settings` table of `kvasir.db`: one row per field changed from its default, keyed by its
//! path, holding its value as JSON. A field with no row has its default, so a new field needs no
//! migration. Values are checked by [`crate::Settings::with_stored`], not here, so one bad row
//! costs only its own field.

use kvasir_db::{Db, DbError, params};
use serde_json::Value;

/// Each stored field's JSON text, by key.
pub async fn load(db: &Db) -> Result<Vec<(String, String)>, DbError> {
    db.call(|c| {
        let mut stmt = c.prepare("SELECT key, value FROM settings ORDER BY key")?;
        stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect()
    })
    .await
}

/// Sets each field in one transaction. `None` removes a field's row, giving it its default.
pub async fn write(db: &Db, changes: Vec<(String, Option<Value>)>) -> Result<(), DbError> {
    db.call(move |c| {
        let tx = c.transaction()?;
        for (key, value) in changes {
            match value {
                None => tx.execute("DELETE FROM settings WHERE key = ?1", params![key])?,
                Some(value) => tx.execute(
                    "INSERT OR REPLACE INTO settings VALUES (?1, ?2)",
                    params![key, value.to_string()],
                )?,
            };
        }
        tx.commit()
    })
    .await
}

/// The `search_cookies` table: each provider and the cookies a person won for it.
pub async fn search_cookies(db: &Db) -> Result<Vec<(String, String)>, DbError> {
    db.call(|c| {
        let mut stmt = c.prepare("SELECT provider, cookies FROM search_cookies")?;
        stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect()
    })
    .await
}

pub async fn set_search_cookies(db: &Db, provider: String, cookies: String) -> Result<(), DbError> {
    db.call(move |c| {
        c.execute(
            "INSERT OR REPLACE INTO search_cookies VALUES (?1, ?2)",
            params![provider, cookies],
        )
        .map(|_| ())
    })
    .await
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[tokio::test]
    async fn written_values_load_back_and_none_removes() {
        let dir = tempfile::tempdir().unwrap();
        let db = Db::open(
            &dir.path().join("kvasir.db"),
            &kvasir_db::Key::for_tests(),
            kvasir_db::schema::KVASIR,
        )
        .unwrap();

        write(&db, vec![("a.b".into(), Some(json!([1, "x"]))), ("c.d".into(), Some(json!(2)))])
            .await
            .unwrap();
        write(&db, vec![("c.d".into(), None)]).await.unwrap();

        assert_eq!(load(&db).await.unwrap(), [("a.b".to_owned(), r#"[1,"x"]"#.to_owned())]);
    }
}
