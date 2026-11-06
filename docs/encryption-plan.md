# Encryption at rest: plan

Status: written 2026-11-06. Phase 1 built 2026-11-06.

Every file in `KVASIR_DATA_DIR` is plain SQLite. Anyone who gets a copy of the directory or a backup
of it reads the runs (questions, prompts, answers, the searches made), the cached pages of what was
researched, and the settings. Once search providers take API keys from the UI
([search-plan.md](search-plan.md)), the settings hold secrets too. This plan encrypts the four
database files with SQLCipher under a key the operator gives in `KVASIR_SECRET_KEY`, which is
required.

## What it protects

| Leak | Protected |
|---|---|
| A copy of `KVASIR_DATA_DIR`, a volume snapshot, a backup | Yes, as long as the key is not stored with it |
| Root on the host, a shell in the container | No: the key is in the environment (`/proc/<pid>/environ`) and in the process's memory |
| A backup that holds the key and the data | No |

A lost key loses the data. There is no recovery, as with n8n's `N8N_ENCRYPTION_KEY` and Forgejo's
`SECRET_KEY`.

## Choice: whole files, not columns

| | SQLCipher, whole file | AEAD per column in the application |
|---|---|---|
| Covers | Every page of the main file, the rollback journal and the WAL | The columns picked |
| Leaks | Page size and access patterns | Primary keys (`pages.url`, `favicons.host`), row counts, lengths, timestamps |
| Queries | Unchanged | Breaks `pieces_fts` (`kvasir-db/src/schema.rs:28`, searched at `kvasir-fetch/src/store.rs:405`), the run lookup by URL (`kvasir-run/src/store.rs:139`, `json_extract(detail, '$.result')`) and the `summary` migration (`schema.rs:15`) |
| Build | A C crypto library linked into SQLite | None: `aws-lc-rs` 1.19.0 is already in `Cargo.lock` through rustls |

The URLs and pages of what a user researched are sensitive, so per-column encryption would leave the
part that matters in the clear. SQLCipher encrypts it and keeps the queries.

API keys get no second layer of their own. The file is encrypted, the settings API never returns a
secret ([search-plan.md](search-plan.md), field kind `secret`), and a second key hierarchy would add
code for no leak it closes.

## SQLCipher

From the SQLCipher design page (https://www.zetetic.net/sqlcipher/design/): AES-256-CBC with an
HMAC-SHA512 per page, a random IV per page, PBKDF2-HMAC-SHA512 with 256,000 iterations to turn a
passphrase into a key. The main file, the journal and the WAL are encrypted. Temporary files are
not, and the page recommends keeping them in memory.

rusqlite 0.41 has two features for it (raw `Cargo.toml` on master):

- `bundled-sqlcipher`: compiles the bundled SQLCipher with `-DSQLITE_HAS_CODEC` and links the
  system `libcrypto`, from `OPENSSL_LIB_DIR` and `OPENSSL_INCLUDE_DIR` when set.
- `bundled-sqlcipher-vendored-openssl`: the same, with OpenSSL built from source by
  `openssl-sys/vendored`.

The image has no OpenSSL today: the runtime stage installs only `ca-certificates` and the lockfile
has no `openssl` crate. The vendored feature keeps it that way at run time, at the cost of a longer
build. The binary links OpenSSL statically, so the runtime stage stays as it is. SQLCipher Community
Edition is BSD-style and asks for a notice of Zetetic's and OpenSSL's copyright. `sqlcipher-src` adds
`blessing` and `WTFPL`, which `deny.toml` allows for that crate alone.

SQLite3 Multiple Ciphers (MIT, ChaCha20-Poly1305 by default per its docs, unverified) has no
rusqlite feature, so using it means a fork of `libsqlite3-sys`. Not taken.

## The key

`KVASIR_SECRET_KEY` is 32 random bytes as 64 hex characters:

```sh
openssl rand -hex 32
```

It goes to SQLCipher as a raw key, `PRAGMA key = "x'<64 hex>'"`, which skips PBKDF2: the key is
already random, and the 256,000 iterations would run on every open. The raw-key syntax is from the
SQLCipher API docs and was not confirmed by this plan's research; phase 2 tests it.

One key serves the four files. Each page has its own random IV, so sharing the key across files
reuses no IV.

Startup fails, as it does without `KVASIR_TOKEN`:

| State | Message |
|---|---|
| Unset | `missing required environment variables: KVASIR_SECRET_KEY` (the existing list in `kvasir-config/src/load.rs:53`) |
| Not 64 hex characters | `KVASIR_SECRET_KEY must be 64 hex characters, as from openssl rand -hex 32` |
| Wrong for an existing file | `<file>: KVASIR_SECRET_KEY does not open it; it was encrypted with another key` |

A wrong key shows on the first read: SQLCipher answers `file is not a database`. `Db::open` reads
`sqlite_master` right after `PRAGMA key` and maps that error to the message above.

## Phases

1. Build. Switch the workspace `rusqlite` to `bundled-sqlcipher-vendored-openssl`. The
   `rust:1.99.1-slim-bookworm` stage needs `make` and `perl` for the OpenSSL build (`perl-base` lacks
   `FindBin`). The tests pass unchanged, since SQLCipher without a key is plain SQLite. Commit.
2. Key. `KVASIR_SECRET_KEY` read and validated in `kvasir-config/src/load.rs`, kept out of `Debug`
   like `Provider`'s key. `Db::open` (`kvasir-db/src/lib.rs:48`) takes the key and runs, before
   `journal_mode=wal`: `PRAGMA key`, `PRAGMA temp_store = MEMORY`, then a read of `sqlite_master`.
   Every opener passes it: `kvasir.db` (settings, `RunStore`, `eval`), `pages.db`, `favicons.db`,
   `sources.db`. Tests: a file written with one key fails to open with another; the raw bytes of a
   file contain no plain text written to it. Commit.
3. Existing files. A file that starts with `SQLite format 3\0` is plain. At startup it is
   encrypted in place: `ATTACH '<file>.encrypting' AS encrypted KEY "x'...'"`, `SELECT
   sqlcipher_export('encrypted')`, `DETACH`, copy `user_version`, open the copy with the key and
   compare row counts per table, rename it over the original, remove the old `-wal` and `-shm`.
   The plain file is gone afterwards, so the release notes tell operators to back up the directory
   first. `sqlcipher_export` and the `ATTACH ... KEY` form are from the SQLCipher docs, unverified
   here; the phase tests them. A test starts on a plain fixture of each file and reads it back.
   Commit.
4. Backups. `Db::backup` (`kvasir-db/src/lib.rs:114`) runs `VACUUM INTO` for the pre-Rust upgrade.
   Test whether its output is encrypted under SQLCipher; if not, export with `sqlcipher_export`
   instead. Commit.
5. Measure. Time a run and `pieces_fts` searches on a `pages.db` of realistic size, before and
   after. Record the figures here. Commit.
6. Docs. README: the environment table, the quickstart (`openssl rand -hex 32`), what is
   protected, and that a lost key loses the data. `docs/architecture.md`: the files table
   (`architecture.md:229`). Commit, as a breaking change, since startup now needs the variable.

## Open questions

- Rotation. `PRAGMA rekey` re-encrypts a file under a new key. A `kvasir rekey` command taking the
  old and new keys would do the four files. Not built until asked.
- `KVASIR_SECRET_KEY_FILE`, for Docker secrets, as Forgejo's `SECRET_KEY_URI`. No variable reads a
  file today, `KVASIR_TOKEN` included; if one does, both should.
- The downloaded source lists (`sources-<hash>.txt`, `kvasir-sites/src/sources/lists.rs:111`) stay
  plain. They are public lists and say nothing about a user.
