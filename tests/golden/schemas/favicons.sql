-- user_version 0
CREATE TABLE favicons (host TEXT PRIMARY KEY, body BLOB, content_type TEXT, provider TEXT, fetched_at REAL NOT NULL);
