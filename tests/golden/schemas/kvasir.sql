-- user_version 0
CREATE TABLE calls (run_id TEXT NOT NULL, id INTEGER NOT NULL, content TEXT NOT NULL, PRIMARY KEY (run_id, id));
CREATE TABLE runs (id TEXT PRIMARY KEY, created_at REAL NOT NULL, state TEXT NOT NULL, summary TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE searches (run_id TEXT NOT NULL, id INTEGER NOT NULL, content TEXT NOT NULL, PRIMARY KEY (run_id, id));
