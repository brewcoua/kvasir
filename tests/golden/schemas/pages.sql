-- user_version 0
CREATE TABLE links (page TEXT NOT NULL, position INTEGER NOT NULL, target TEXT NOT NULL, anchor TEXT NOT NULL, context TEXT NOT NULL, region TEXT NOT NULL, PRIMARY KEY (page, position));
CREATE TABLE pages (url TEXT PRIMARY KEY, final_url TEXT NOT NULL, fetched_at REAL NOT NULL, etag TEXT, last_modified TEXT, content_hash TEXT NOT NULL, engine TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL);
CREATE TABLE pieces (page TEXT NOT NULL, position INTEGER NOT NULL, text TEXT NOT NULL, model TEXT NOT NULL, vector BLOB NOT NULL, PRIMARY KEY (page, position));
CREATE VIRTUAL TABLE pieces_fts USING fts5(text, content='pieces', content_rowid='rowid');
CREATE TABLE 'pieces_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID;
CREATE TABLE 'pieces_fts_data'(id INTEGER PRIMARY KEY, block BLOB);
CREATE TABLE 'pieces_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB);
CREATE TABLE 'pieces_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;
CREATE TRIGGER pieces_added AFTER INSERT ON pieces BEGIN INSERT INTO pieces_fts(rowid, text) VALUES (new.rowid, new.text); END;
CREATE TRIGGER pieces_dropped AFTER DELETE ON pieces BEGIN INSERT INTO pieces_fts(pieces_fts, rowid, text) VALUES ('delete', old.rowid, old.text); END;
