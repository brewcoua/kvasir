-- user_version 0
CREATE TABLE marks (host TEXT PRIMARY KEY, value TEXT NOT NULL, marked_at REAL NOT NULL);
CREATE TABLE sources (host TEXT PRIMARY KEY, kind TEXT, tags TEXT, label TEXT, qid TEXT, fetched_at REAL NOT NULL);
